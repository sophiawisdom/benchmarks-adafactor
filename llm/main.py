# Copyright 2022 MosaicML Benchmarks authors
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import warnings

from composer import Trainer
from composer.callbacks import LRMonitor, MemoryMonitor, SpeedMonitor, CheckpointSaver
from composer.loggers import WandBLogger, RemoteUploaderDownloader
from composer.optim import DecoupledAdamW
from composer.algorithms import SelectiveBackprop
from torch_optimizer import Adafactor
from composer.optim.scheduler import (ConstantWithWarmupScheduler,
                                      CosineAnnealingWithWarmupScheduler)
from composer.utils import dist, reproducibility
from omegaconf import OmegaConf as om
from src.data_c4 import build_c4_dataloader
from src.lm_harness_evaluation_callback import EvaluationCallback
from src.model_registry import COMPOSER_MODEL_REGISTRY
from src.mosaic_gpt import ComposerMosaicGPT


def build_object_store_loader(kwargs):
    if kwargs is None:
        return None
    return RemoteUploaderDownloader(
        bucket_uri=f"libcloud://{kwargs['bucket']}",
        backend_kwargs={
            "provider": "google_storage",
            "container": kwargs['bucket'],
            "key_environ": "GCS_KEY", # Name of env variable for HMAC access id.
            "secret_environ": "GCS_SECRET", # Name of env variable for HMAC secret.
        },
    )


def build_logger(name, kwargs):
    if name == 'wandb':
        return WandBLogger(**kwargs)
    elif name == "remote_uploader_downloader":
        return build_object_store_loader(kwargs)
    else:
        raise ValueError(f'Not sure how to build logger: {name}')


def build_callback(name, kwargs):
    if name == 'lr_monitor':
        return LRMonitor()
    elif name == 'memory_monitor':
        return MemoryMonitor()
    elif name == 'speed_monitor':
        return SpeedMonitor(window_size=kwargs.get('window_size', 1))
    elif name == "lm_eval_harness":
        return EvaluationCallback(every_n_batches=kwargs.get("every_n_batches", 32))
    elif name == "checkpoint_saver":
        return CheckpointSaver(folder="sophia_model_experiments/{run_name}-checkpoints", save_interval=kwargs.get("save_interval", "100ba"))
    else:
        raise ValueError(f'Not sure how to build callback: {name}')


def build_optimizer(cfg, model):
    if cfg.name == 'decoupled_adamw':
        return DecoupledAdamW(
            model.parameters(),
            lr=cfg.lr,
            betas=cfg.betas,
            eps=cfg.eps,
            weight_decay=cfg.weight_decay
        )
    elif cfg.name == "Adafactor":
        return Adafactor(model.parameters(), lr=cfg.lr)
    else:
        raise ValueError(f'Not sure how to build optimizer: {cfg.name}')

def build_algorithm(name, cfg):
    if name == "selective_backprop":
        return SelectiveBackprop(start=cfg.start, end=cfg.end, interrupt=cfg.interrupt, keep=cfg.keep)
    else:
        raise ValueError(f"Not sure how to build algorithm: {name}")

def build_scheduler(cfg):
    if cfg.name == 'constant_with_warmup':
        return ConstantWithWarmupScheduler(t_warmup=cfg.t_warmup)
    elif cfg.name == 'cosine_with_warmup':
        return CosineAnnealingWithWarmupScheduler(t_warmup=cfg.t_warmup,
                                                  alpha_f=cfg.alpha_f)
    else:
        raise ValueError(f'Not sure how to build scheduler: {cfg.name}')


def calculate_batch_size_info(global_batch_size, device_microbatch_size):
    if global_batch_size % dist.get_world_size() != 0:
        raise ValueError(f'Global batch size {global_batch_size} is not divisible by {dist.get_world_size()} '
                         'as a result, the batch size would be truncated, please adjust `global_batch_size` '
                         f'to be divisible by world size, {dist.get_world_size()}.')
    device_batch_size = global_batch_size // dist.get_world_size()
    if device_microbatch_size == 'auto':
        device_grad_accum = 'auto'
    elif isinstance(device_microbatch_size, int):
        if device_microbatch_size > device_batch_size:
            print(
                f'WARNING: device_microbatch_size > device_batch_size, '
                f'will be reduced from {device_microbatch_size} -> {device_batch_size}.'
            )
            device_microbatch_size = device_batch_size
        device_grad_accum = device_batch_size // device_microbatch_size
    else:
        raise ValueError(f'Not sure how to parse {device_microbatch_size=}')

    return device_batch_size, device_microbatch_size, device_grad_accum


# Coming soon: this conversion math will be done inside Composer Trainer
def update_batch_size_info(cfg):
    device_train_batch_size, device_train_microbatch_size, device_train_grad_accum = calculate_batch_size_info(
        cfg.global_train_batch_size, cfg.device_train_microbatch_size)
    cfg.n_gpus = dist.get_world_size()
    cfg.device_train_batch_size = device_train_batch_size
    cfg.device_train_microbatch_size = device_train_microbatch_size
    cfg.device_train_grad_accum = device_train_grad_accum
    # Safely set `device_eval_batch_size` if not provided by user
    if 'device_eval_batch_size' not in cfg:
        if cfg.device_train_microbatch_size == 'auto':
            cfg.device_eval_batch_size = 1  # TODO debug auto eval microbatching
        else:
            cfg.device_eval_batch_size = cfg.device_train_microbatch_size
    return cfg


def log_config(cfg):
    print(om.to_yaml(cfg))
    if 'wandb' in cfg.get('loggers', {}):
        try:
            import wandb
        except ImportError as e:
            raise e
        if wandb.run:
            wandb.config.update(om.to_container(cfg, resolve=True))


def build_composer_model(cfg):
    warnings.filterwarnings(
        action='ignore',
        message='Torchmetrics v0.9 introduced a new argument class property')
    try:
        return COMPOSER_MODEL_REGISTRY[cfg.name](cfg)
    except:
        raise ValueError(f'Not sure how to build model with name={cfg.name}')


def build_dataloader(cfg, device_batch_size, shuffle_seed=None):
    if cfg.name == 'c4':
        return build_c4_dataloader(cfg, device_batch_size, shuffle_seed=shuffle_seed)
    else:
        raise ValueError(f'Not sure how to build model with name={cfg.name}')


def main(cfg):
    reproducibility.seed_all(cfg.seed)

    # Run Name
    cfg.run_name = cfg.get('run_name', os.environ.get('COMPOSER_RUN_NAME',
                                                      'llm'))

    # Get batch size info
    cfg = update_batch_size_info(cfg)

    # Read FSDP Config as a dict
    fsdp_config = cfg.get('fsdp_config', None)
    fsdp_config = om.to_container(fsdp_config,
                                  resolve=True) if fsdp_config else None

    # Build Model
    # For fast initialization of MosaicGPT, use cfg.model.device='meta'
    print('Initializing model...')
    model = build_composer_model(cfg.model)
    cfg.n_params = sum(p.numel() for p in model.parameters())
    print(f'{cfg.n_params=:.2e}')

    # Dataloaders
    print('Building train loader...')
    train_loader = build_dataloader(cfg.train_loader,
                                    cfg.device_train_batch_size,
                                    shuffle_seed=cfg.seed)
    print('Building eval loader...')
    eval_loader = build_dataloader(cfg.eval_loader, cfg.device_eval_batch_size)

    # Optimizer
    optimizer = build_optimizer(cfg.optimizer, model)

    # Scheduler
    scheduler = build_scheduler(cfg.scheduler)

    # Loggers
    loggers = [
        build_logger(name, logger_cfg)
        for name, logger_cfg in cfg.get('loggers', {}).items()
    ]

    # Callbacks
    callbacks = [
        build_callback(name, callback_cfg)
        for name, callback_cfg in cfg.get('callbacks', {}).items()
    ]

    algorithms = [build_algorithm(name, algorithm_cfg) for name, algorithm_cfg in cfg.get("algorithms", {}).items()]

    # Build the Trainer
    trainer = Trainer(
        run_name=cfg.run_name,
        seed=cfg.seed,
        model=model,
        algorithms=algorithms,
        train_dataloader=train_loader,
        eval_dataloader=eval_loader,
        optimizers=optimizer,
        schedulers=scheduler,
        max_duration=cfg.max_duration,
        eval_interval=cfg.eval_interval,
        eval_subset_num_batches=cfg.eval_loader.get('eval_subset_num_batches',
                                                    -1),
        progress_bar=cfg.progress_bar,
        log_to_console=cfg.log_to_console,
        loggers=loggers,
        callbacks=callbacks,
        precision=cfg.precision,
        grad_clip_norm=cfg.grad_clip_norm,
        grad_accum=cfg.device_train_grad_accum,
        fsdp_config=fsdp_config,  # type: ignore
        save_folder=cfg.get('save_folder', None),
        save_interval=cfg.get('save_interval', '1000ba'),
        save_num_checkpoints_to_keep=cfg.get('save_num_checkpoints_to_keep',
                                             -1),
        load_path=cfg.get('load_path', None),
        load_weights_only=cfg.get('load_weights_only', False),
        load_object_store=build_object_store_loader(cfg.get('load_object_store', None)),
        save_overwrite=cfg.get('save_overwrite', False),
    )

    while trainer.state.timestamp.batch < cfg.get("resume_batch", 0):
        trainer.state.timestamp = trainer.state.timestamp.to_next_batch()
        
    print("Logging config...")
    log_config(cfg)

    print('Starting training...')
    trainer.fit()

    print('Done.')


if __name__ == '__main__':
    yaml_path, args_list = sys.argv[1], sys.argv[2:]
    with open(yaml_path) as f:
        yaml_cfg = om.load(f)
    cli_cfg = om.from_cli(args_list)
    cfg = om.merge(yaml_cfg, cli_cfg)
    orig_run_name = cfg.get('run_name', os.environ.get('COMPOSER_RUN_NAME', 'llm'))
    if cfg.get("lrs"):
        for lr in cfg.lrs:
            cfg.optimizer.lr = lr
            cfg.run_name = orig_run_name + str(lr)
            print("Running learning rate", lr)
            main(cfg)
    else:
        main(cfg)
