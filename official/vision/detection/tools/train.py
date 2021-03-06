# -*- coding: utf-8 -*-
# MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
#
# Copyright (c) 2014-2020 Megvii Inc. All rights reserved.
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
import argparse
import bisect
import copy
import functools
import importlib
import multiprocessing as mp
import os
import sys
import time
from tabulate import tabulate

import numpy as np

import megengine as mge
from megengine import distributed as dist
from megengine import jit
from megengine import optimizer as optim
from megengine.data import DataLoader, Infinite, RandomSampler
from megengine.data import transform as T

from official.vision.detection.tools.data_mapper import data_mapper
from official.vision.detection.tools.utils import (
    AverageMeter,
    DetectionPadCollator,
    GroupedRandomSampler
)

logger = mge.get_logger(__name__)


def make_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f", "--file", default="net.py", type=str, help="net description file"
    )
    parser.add_argument(
        "-w", "--weight_file", default=None, type=str, help="weights file",
    )
    parser.add_argument(
        "-n", "--ngpus", default=-1, type=int, help="total number of gpus for training",
    )
    parser.add_argument(
        "-b", "--batch_size", default=2, type=int, help="batchsize for training",
    )
    parser.add_argument(
        "-d", "--dataset_dir", default="/data/datasets", type=str,
    )
    parser.add_argument("--enable_sublinear", action="store_true")

    return parser


def main():
    parser = make_parser()
    args = parser.parse_args()

    # ------------------------ begin training -------------------------- #
    valid_nr_dev = mge.get_device_count("gpu")
    if args.ngpus == -1:
        world_size = valid_nr_dev
    else:
        if args.ngpus > valid_nr_dev:
            logger.error("do not have enough gpus for training")
            sys.exit(1)
        else:
            world_size = args.ngpus

    logger.info("Device Count = %d", world_size)

    log_dir = "log-of-{}".format(os.path.basename(args.file).split(".")[0])
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)

    if world_size > 1:
        mp.set_start_method("spawn")
        processes = list()
        for i in range(world_size):
            process = mp.Process(target=worker, args=(i, world_size, args))
            process.start()
            processes.append(process)

        for p in processes:
            p.join()
    else:
        worker(0, 1, args)


def worker(rank, world_size, args):
    if world_size > 1:
        dist.init_process_group(
            master_ip="localhost",
            master_port=23456,
            world_size=world_size,
            rank=rank,
            dev=rank,
        )
        logger.info("Init process group for gpu%d done", rank)

    sys.path.insert(0, os.path.dirname(args.file))
    current_network = importlib.import_module(os.path.basename(args.file).split(".")[0])

    model = current_network.Net(current_network.Cfg(), batch_size=args.batch_size)
    params = model.parameters(requires_grad=True)
    model.train()

    if rank == 0:
        logger.info(get_config_info(model.cfg))
    opt = optim.SGD(
        params,
        lr=model.cfg.basic_lr * world_size * model.batch_size,
        momentum=model.cfg.momentum,
        weight_decay=model.cfg.weight_decay,
    )

    if args.weight_file is not None:
        weights = mge.load(args.weight_file)
        model.backbone.bottom_up.load_state_dict(weights)

    if rank == 0:
        logger.info("Prepare dataset")
    train_loader = iter(build_dataloader(model.batch_size, args.dataset_dir, model.cfg))

    for epoch_id in range(model.cfg.max_epoch):
        for param_group in opt.param_groups:
            param_group["lr"] = (
                model.cfg.basic_lr
                * world_size
                * model.batch_size
                * (
                    model.cfg.lr_decay_rate
                    ** bisect.bisect_right(model.cfg.lr_decay_stages, epoch_id)
                )
            )

        tot_steps = model.cfg.nr_images_epoch // (model.batch_size * world_size)
        train_one_epoch(
            model,
            train_loader,
            opt,
            tot_steps,
            rank,
            epoch_id,
            world_size,
            args.enable_sublinear,
        )
        if rank == 0:
            save_path = "log-of-{}/epoch_{}.pkl".format(
                os.path.basename(args.file).split(".")[0], epoch_id
            )
            mge.save(
                {"epoch": epoch_id, "state_dict": model.state_dict()}, save_path,
            )
            logger.info("dump weights to %s", save_path)


def train_one_epoch(
    model,
    data_queue,
    opt,
    tot_steps,
    rank,
    epoch_id,
    world_size,
    enable_sublinear=False,
):
    sublinear_cfg = jit.SublinearMemoryConfig() if enable_sublinear else None

    @jit.trace(symbolic=True, sublinear_memory_config=sublinear_cfg)
    def propagate():
        loss_dict = model(model.inputs)
        opt.backward(loss_dict["total_loss"])
        losses = list(loss_dict.values())
        return losses

    meter = AverageMeter(record_len=model.cfg.num_losses)
    time_meter = AverageMeter(record_len=2)
    log_interval = model.cfg.log_interval
    for step in range(tot_steps):
        adjust_learning_rate(opt, epoch_id, step, model, world_size)

        data_tik = time.time()
        mini_batch = next(data_queue)
        data_tok = time.time()

        model.inputs["image"].set_value(mini_batch["data"])
        model.inputs["gt_boxes"].set_value(mini_batch["gt_boxes"])
        model.inputs["im_info"].set_value(mini_batch["im_info"])

        tik = time.time()
        opt.zero_grad()
        loss_list = propagate()
        opt.step()
        tok = time.time()

        time_meter.update([tok - tik, data_tok - data_tik])

        if rank == 0:
            info_str = "e%d, %d/%d, lr:%f, "
            loss_str = ", ".join(
                ["{}:%f".format(loss) for loss in model.cfg.losses_keys]
            )
            time_str = ", train_time:%.3fs, data_time:%.3fs"
            log_info_str = info_str + loss_str + time_str
            meter.update([loss.numpy() for loss in loss_list])
            if step % log_interval == 0:
                average_loss = meter.average()
                logger.info(
                    log_info_str,
                    epoch_id,
                    step,
                    tot_steps,
                    opt.param_groups[0]["lr"],
                    *average_loss,
                    *time_meter.average()
                )
                meter.reset()
                time_meter.reset()


def get_config_info(config):
    config_table = []
    for c, v in config.__dict__.items():
        if not isinstance(v, (int, float, str, list, tuple, dict, np.ndarray)):
            if hasattr(v, "__name__"):
                v = v.__name__
            elif hasattr(v, "__class__"):
                v = v.__class__
            elif isinstance(v, functools.partial):
                v = v.func.__name__
        config_table.append((str(c), str(v)))
    config_table = tabulate(config_table)
    return config_table


def adjust_learning_rate(optimizer, epoch_id, step, model, world_size):
    base_lr = (
        model.cfg.basic_lr
        * world_size
        * model.batch_size
        * (
            model.cfg.lr_decay_rate
            ** bisect.bisect_right(model.cfg.lr_decay_stages, epoch_id)
        )
    )
    # Warm up
    if epoch_id == 0 and step < model.cfg.warm_iters:
        lr_factor = (step + 1.0) / model.cfg.warm_iters
        for param_group in optimizer.param_groups:
            param_group["lr"] = base_lr * lr_factor


def build_dataset(data_dir, cfg):
    data_cfg = copy.deepcopy(cfg.train_dataset)
    data_name = data_cfg.pop("name")

    data_cfg["root"] = os.path.join(data_dir, data_name, data_cfg["root"])

    if "ann_file" in data_cfg:
        data_cfg["ann_file"] = os.path.join(data_dir, data_name, data_cfg["ann_file"])

    data_cfg["order"] = ["image", "boxes", "boxes_category", "info"]

    return data_mapper[data_name](**data_cfg)


def build_sampler(train_dataset, batch_size, aspect_grouping=[1]):
    def _compute_aspect_ratios(dataset):
        aspect_ratios = []
        for i in range(len(dataset)):
            info = dataset.get_img_info(i)
            aspect_ratios.append(info["height"] / info["width"])
        return aspect_ratios

    def _quantize(x, bins):
        return list(map(lambda y: bisect.bisect_right(sorted(bins), y), x))

    if len(aspect_grouping) == 0:
        return Infinite(RandomSampler(train_dataset, batch_size, drop_last=True))

    aspect_ratios = _compute_aspect_ratios(train_dataset)
    group_ids = _quantize(aspect_ratios, aspect_grouping)
    return Infinite(GroupedRandomSampler(train_dataset, batch_size, group_ids))


def build_dataloader(batch_size, data_dir, cfg):
    train_dataset = build_dataset(data_dir, cfg)
    train_sampler = build_sampler(train_dataset, batch_size)
    train_dataloader = DataLoader(
        train_dataset,
        sampler=train_sampler,
        transform=T.Compose(
            transforms=[
                T.ShortestEdgeResize(
                    cfg.train_image_short_size,
                    cfg.train_image_max_size,
                    sample_style="choice",
                ),
                T.RandomHorizontalFlip(),
                T.ToMode(),
            ],
            order=["image", "boxes", "boxes_category"],
        ),
        collator=DetectionPadCollator(),
        num_workers=2,
    )
    return train_dataloader


if __name__ == "__main__":
    main()
