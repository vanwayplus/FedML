#!/usr/bin/env bash

WORKER_NUM=$1
STAGE=$2

PROCESS_NUM=`expr $WORKER_NUM + 1`
echo $PROCESS_NUM

hostname > mpi_host_file

$(which mpirun) -np $PROCESS_NUM \
-hostfile mpi_host_file \
python torch_fednas_cifar10_dart_step_by_step_example.py --cf config/train_config.yaml