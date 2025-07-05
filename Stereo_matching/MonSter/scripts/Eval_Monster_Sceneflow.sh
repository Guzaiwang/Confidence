CUDA_VISIBLE_DEVICES=2 python evaluate_stereo.py \
    --restore_ckpt ./pretrained/sceneflow.pth \
    --dataset sceneflow 
    
    
# >> ./scripts/logs/0303_evaluate_stereo.txt 2>&1 &