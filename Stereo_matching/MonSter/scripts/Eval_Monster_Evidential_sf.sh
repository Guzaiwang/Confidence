#!/bin/bash

# 设置基础路径
BASE_PATH="/DATA/i2r/guzw/workspace/Harmonious_confidence/Stereo_matching/MonSter/checkpoints/train_Monster_evidential_sceneflow_v0"
LOG_DIR="./outputs/EVAL_LOGS/Monster_evidential_sceneflow_v0"
SCRIPT="evaluate_HILC_sceneflow.py"
MAIN_LOG="${LOG_DIR}/evaluation_progress.log"

# 确保日志目录存在
mkdir -p $LOG_DIR

# 记录开始时间
echo "Starting evaluation of checkpoints from 2000 to 40000 at $(date)" > $MAIN_LOG
gpu_id=4
# 从6000开始，每次增加2000，一直到22000
for checkpoint in $(seq 28000 4000 40000); do
    echo "Evaluating checkpoint: ${checkpoint}.pth" >> $MAIN_LOG
    # 构建检查点文件路径
    CKPT_PATH="${BASE_PATH}/${checkpoint}.pth"
    
    # 构建日志文件名
    LOG_FILE="${LOG_DIR}/Eval_${checkpoint}.txt"
    
    # 运行评估命令
    CUDA_VISIBLE_DEVICES=$gpu_id python $SCRIPT \
        --restore_ckpt $CKPT_PATH \
        --dataset sceneflow >> $LOG_FILE 2>&1 &
    
    echo "Evaluation for ${checkpoint}.pth completed. Log saved to ${LOG_FILE}" >> $MAIN_LOG

    gpu_id=$((gpu_id + 1))

done

echo "All evaluations completed at $(date)" >> $MAIN_LOG