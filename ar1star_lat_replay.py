#!/usr/bin/env python
# -*- coding: utf-8 -*-

################################################################################
# Copyright (c) 2019. Vincenzo Lomonaco. All rights reserved.                  #
# See the accompanying LICENSE file for terms.                                 #
#                                                                              #
# Date: 23-07-2019                                                             #
# Author: Vincenzo Lomonaco                                                    #
# E-mail: vincenzo.lomonaco@unibo.it                                           #
# Website: vincenzolomonaco.com                                                #
################################################################################

""" Simple AR1* implementation in PyTorch with Latent Replay """

# Python 2-3 compatible
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

from core50.dataset import CORE50
import torch
import numpy as np
import copy
import os
import json
from models.mobilenet import MyMobilenetV1
from utils.common import *
from utils.train_test import *
import tensorflow as tf
import time
import configparser
import argparse
from pprint import pprint
from torch.utils.tensorboard import SummaryWriter

# --------------------------------- Setup --------------------------------------

# recover exp configuration name
parser = argparse.ArgumentParser(description='Run CL experiments')
parser.add_argument('--cfg', dest='exp_name',  default='NIC',
                    help='name of the experiment you want to run.')
args = parser.parse_args()

# set cuda device (based on your hardware)
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "0"

# recover config file for the experiment
config = configparser.ConfigParser()
config.read("params.cfg")
exp_config = config[args.exp_name]
print("Experiment name:", args.exp_name)
pprint(dict(exp_config))

# recover parameters from the cfg file and compute the dependent ones.
exp_name = eval(exp_config['exp_name'])
comment = eval(exp_config['comment'])
use_cuda = eval(exp_config['use_cuda'])
init_lr = eval(exp_config['init_lr'])
inc_lr = eval(exp_config['inc_lr'])
mb_size = eval(exp_config['mb_size'])
init_train_ep = eval(exp_config['init_train_ep'])
inc_train_ep = eval(exp_config['inc_train_ep'])
init_update_rate = eval(exp_config['init_update_rate'])
inc_update_rate = eval(exp_config['inc_update_rate'])
max_r_max = eval(exp_config['max_r_max'])
max_d_max = eval(exp_config['max_d_max'])
inc_step = eval(exp_config['inc_step'])
rm_sz = eval(exp_config['rm_sz'])
momentum = eval(exp_config['momentum'])
l2 = eval(exp_config['l2'])
freeze_below_layer = eval(exp_config['freeze_below_layer'])
latent_layer_num = eval(exp_config['latent_layer_num'])
reg_lambda = eval(exp_config['reg_lambda'])
scenario = eval(exp_config['scenario'])
sub_dir = scenario

# setting up log dir for tensorboard
log_dir = 'logs/' + exp_name
writer = SummaryWriter(log_dir)

# Saving params
hyper = json.dumps(dict(exp_config))
writer.add_text("parameters", hyper, 0)

# Other variables init
tot_it_step = 0
rm = None

# do not remove this line
start_time = time.time()

# Create the dataset object
dataset = CORE50(root='/home/admin/ssd_data/cvpr_competition/cvpr_competition_data/', scenario=scenario, preload=False)
preproc = preprocess_imgs

# Get the fixed test set
full_valdidset = dataset.get_full_valid_set()

# Model setup
model = MyMobilenetV1(pretrained=True, latent_layer_num=latent_layer_num)
replace_bn_with_brn(
    model, momentum=init_update_rate, r_d_max_inc_step=inc_step,
    max_r_max=max_r_max, max_d_max=max_d_max
)
model.saved_weights = {}
model.past_j = {i:0 for i in range(50)}
model.cur_j = {i:0 for i in range(50)}
if reg_lambda != 0:
    ewcData, synData = create_syn_data(model)

# Optimizer setup
optimizer = torch.optim.SGD(
    model.parameters(), lr=init_lr, momentum=momentum, weight_decay=l2
)
criterion = torch.nn.CrossEntropyLoss()

# --------------------------------- Training -----------------------------------

# vars to update over time
valid_acc = []
ext_mem_sz = []
ram_usage = []
heads = []
ext_mem = None
stats = {"ram": [], "disk": []}

# loop over the training incremental batches
for i, train_batch in enumerate(dataset):

    if reg_lambda != 0:
        init_batch(model, ewcData, synData)

    # we freeze the layer below the replay layer since the first batch
    freeze_up_to(model, freeze_below_layer, only_conv=False)

    if i == 1:
        change_brn_pars(
            model, momentum=inc_update_rate, r_d_max_inc_step=0,
            r_max=max_r_max, d_max=max_d_max)
        optimizer = torch.optim.SGD(
            model.parameters(), lr=inc_lr, momentum=momentum, weight_decay=l2
        )

    train_x, train_y, t = train_batch
    train_x = preproc(train_x)

    if i == 0:
        cur_class = [int(o) for o in set(train_y)]
        model.cur_j = examples_per_class(train_y)
    else:
        cur_class = [int(o) for o in set(train_y).union(set(rm[1]))]
        model.cur_j = examples_per_class(list(train_y) + list(rm[1]))

    print("----------- batch {0} -------------".format(i))
    print("train_x shape: {}, train_y shape: {}"
          .format(train_x.shape, train_y.shape))

    model.train()
    model.lat_features.eval()

    reset_weights(model, cur_class)
    cur_ep = 0

    if i == 0:
        (train_x, train_y), it_x_ep = pad_data([train_x, train_y], mb_size)
    shuffle_in_unison([train_x, train_y], in_place=True)

    model = maybe_cuda(model, use_cuda=use_cuda)
    acc = None
    ave_loss = 0

    train_x = torch.from_numpy(train_x).type(torch.FloatTensor)
    train_y = torch.from_numpy(train_y).type(torch.LongTensor)

    if i == 0:
        train_ep = init_train_ep
    else:
        train_ep = inc_train_ep

    for ep in range(train_ep):

        stats['disk'].append(check_ext_mem("cl_ext_mem"))
        stats['ram'].append(check_ram_usage())

        print("training ep: ", ep)
        correct_cnt, ave_loss = 0, 0

        if i > 0:
            cur_sz = train_x.size(0) // ((train_x.size(0) + rm_sz) // mb_size)
            it_x_ep = train_x.size(0) // cur_sz
            n2inject = max(0, mb_size - cur_sz)
        else:
            n2inject = 0
        print("total sz:", train_x.size(0) + rm_sz)
        print("n2inject", n2inject)
        print("it x ep: ", it_x_ep)

        for it in range(it_x_ep):

            if reg_lambda !=0:
                pre_update(model, synData)

            start = it * (mb_size - n2inject)
            end = (it + 1) * (mb_size - n2inject)

            optimizer.zero_grad()

            x_mb = maybe_cuda(train_x[start:end], use_cuda=use_cuda)

            if i == 0:
                lat_mb_x = None
                y_mb = maybe_cuda(train_y[start:end], use_cuda=use_cuda)

            else:
                lat_mb_x = rm[0][it*n2inject: (it + 1)*n2inject]
                lat_mb_y = rm[1][it*n2inject: (it + 1)*n2inject]
                y_mb = maybe_cuda(
                    torch.cat((train_y[start:end], lat_mb_y), 0),
                    use_cuda=use_cuda)
                lat_mb_x = maybe_cuda(lat_mb_x, use_cuda=use_cuda)

            logits, lat_acts = model(
                x_mb, latent_input=lat_mb_x, return_lat_acts=True)

            # collect latent volumes only for the first ep
            if ep == 0:
                lat_acts = lat_acts.cpu().detach()
                if it == 0:
                    cur_acts = copy.deepcopy(lat_acts)
                else:
                    cur_acts = torch.cat((cur_acts, lat_acts), 0)

            _, pred_label = torch.max(logits, 1)
            correct_cnt += (pred_label == y_mb).sum()

            loss = criterion(logits, y_mb)
            if reg_lambda !=0:
                loss += compute_ewc_loss(model, ewcData, lambd=reg_lambda)
            ave_loss += loss.item()

            loss.backward()
            optimizer.step()

            if reg_lambda !=0:
                post_update(model, synData)

            acc = correct_cnt.item() / \
                  ((it + 1) * y_mb.size(0))
            ave_loss /= ((it + 1) * y_mb.size(0))

            if it % 10 == 0:
                print(
                    '==>>> it: {}, avg. loss: {:.6f}, '
                    'running train acc: {:.3f}'
                        .format(it, ave_loss, acc)
                )

            # Log scalar values (scalar summary) to TB
            tot_it_step +=1
            writer.add_scalar('train_loss', ave_loss, tot_it_step)
            writer.add_scalar('train_accuracy', acc, tot_it_step)

        cur_ep += 1

    consolidate_weights(model, cur_class)
    if reg_lambda != 0:
        update_ewc_data(model, ewcData, synData, 0.001, 1)

    # how many patterns to save for next iter
    h = min(rm_sz // (i + 1), cur_acts.size(0))
    print("h", h)

    print("cur_acts sz:", cur_acts.size(0))
    idxs_cur = np.random.choice(
        cur_acts.size(0), h, replace=False
    )
    rm_add = [cur_acts[idxs_cur], train_y[idxs_cur]]
    print("rm_add size", rm_add[0].size(0))

    # replace patterns in random memory
    if i == 0:
        rm = copy.deepcopy(rm_add)
    else:
        idxs_2_replace = np.random.choice(
            rm[0].size(0), h, replace=False
        )
        for j, idx in enumerate(idxs_2_replace):
            rm[0][idx] = copy.deepcopy(rm_add[0][j])
            rm[1][idx] = copy.deepcopy(rm_add[1][j])

    set_consolidate_weights(model)

    if scenario == "multi-task-nc":
        heads.append(copy.deepcopy(model.output))

    # collect statistics
    ext_mem_sz += stats['disk']
    ram_usage += stats['ram']

    stats_test, _ = test_multitask(
        model, full_valdidset, mb_size,
        preproc=preprocess_imgs, multi_heads=heads, verbose=False
    )

    # Log scalar values (scalar summary) to TB
    writer.add_scalar('test_loss', ave_loss, i)
    writer.add_scalar('test_accuracy', acc, i)

    # update number examples encountered over time
    for c, n in model.cur_j.items():
        model.past_j[c] += n

    valid_acc += stats_test['acc']
    print("---------------------------------")
    print("Accuracy: ", stats_test['acc'])
    print("---------------------------------")

# directory with the code snapshot to generate the results
sub_dir = 'submissions/' + sub_dir
if not os.path.exists(sub_dir):
    os.makedirs(sub_dir)

# copy code
create_code_snapshot(".", sub_dir + "/code_snapshot")

# generating metadata.txt: with all the data used for the CLScore
elapsed = (time.time() - start_time) / 60
print("Training Time: {}m".format(elapsed))
with open(sub_dir + "/metadata.txt", "w") as wf:
    for obj in [
        np.average(valid_acc), elapsed, np.average(ram_usage),
        np.max(ram_usage), np.average(ext_mem_sz), np.max(ext_mem_sz)
    ]:
        wf.write(str(obj) + "\n")

# test_preds.txt: with a list of labels separated by "\n"
print("Final inference on test set...")
full_testset = dataset.get_full_test_set()
stats, preds = test_multitask(
    model, full_testset, mb_size, preproc=preprocess_imgs,
    multi_heads=heads, verbose=False
)

with open(sub_dir + "/test_preds.txt", "w") as wf:
    for pred in preds:
        wf.write(str(pred) + "\n")

print("Experiment completed.")