rm -rf ./outputs/logs/0705_HILC_v0_DGX3_4567.txt
CUDA_VISIBLE_DEVICES=4,5,6,7 accelerate launch train_HILC_sceneflow_fp16_v0.py >> ./outputs/logs/0705_HILC_v0_DGX3_4567.txt 2>&1 &