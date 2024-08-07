#!/bin/bash
#SBATCH -J DongDong # Job name
#SBATCH -o para.log # SLURM standard output to file
#SBATCH -e para.log # SLURM standard error to file
#SBATCH --nodes=1 # Require nodes to be used
#SBATCH --ntasks-per-node=1 # tasks/processes 
#SBATCH --cpus-per-task=2 #  CPU cores to be used 
#SBATCH --gres=gpu:1 # Require 8 GPUs to be used
#SBATCH --mem=128G # memory required per node
#SBATCH --time=06:00:00 # Set time of the job
#SBATCH -p defq # Partition/Queue name

#==========================
# Load modules
#==========================
module purge
module load slurm/slurm/23.02.4
module load nvidia-hpc/2024_241
module load nvhpc/24.1
module list
#==========================
# Execute My Program
#==========================
# srun conda init
# srun conda activate RLR
# srun python3 PrefixPretrain.py
# srun python3 EncLM_inference.py
srun python3 RL_judge.py