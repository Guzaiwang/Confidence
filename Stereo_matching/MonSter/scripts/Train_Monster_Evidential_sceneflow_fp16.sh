rm -rf ./outputs/logs/0705_Monster_Evi_v0_DGX4_4567.txt
CUDA_VISIBLE_DEVICES=4,5,6,7 accelerate launch train_Monster_evidential_sceneflow.py >> ./outputs/logs/0705_Monster_Evi_v0_DGX4_4567.txt 2>&1 &