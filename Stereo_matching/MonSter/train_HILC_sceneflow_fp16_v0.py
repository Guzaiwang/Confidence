import os
import hydra
import torch
from tqdm import tqdm
import torch.optim as optim
# from util import InputPadder
from core.utils.utils import InputPadder
from core.monster_HILC import Monster 
from omegaconf import OmegaConf
import torch.nn.functional as F
from accelerate import Accelerator
import core.stereo_datasets as datasets
from accelerate.utils import set_seed
from accelerate.logging import get_logger
from accelerate import DataLoaderConfiguration
from accelerate.utils import DistributedDataParallelKwargs


import matplotlib
from einops import rearrange
import matplotlib.pyplot as plt
import numpy as np
import wandb
wandb.init(project="td_depthstereo")
from pathlib import Path
# torch.autograd.set_detect_anomaly(True)
def gray_2_colormap_np(img, cmap = 'rainbow', max = None):
    img = img.cpu().detach().numpy().squeeze()
    assert img.ndim == 2
    img[img<0] = 0
    mask_invalid = img < 1e-10
    if max == None:
        img = img / (img.max() + 1e-8)
    else:
        img = img/(max + 1e-8)

    norm = matplotlib.colors.Normalize(vmin=0, vmax=1.1)
    cmap_m = matplotlib.cm.get_cmap(cmap)
    map = matplotlib.cm.ScalarMappable(norm=norm, cmap=cmap_m)
    colormap = (map.to_rgba(img)[:,:,:3]*255).astype(np.uint8)
    colormap[mask_invalid] = 0

    return colormap

def sequence_loss(disp_preds, disp_init_pred, disp_gt, valid, loss_gamma=0.9, max_disp=192):
    """ Loss function defined over sequence of flow predictions """

    n_predictions = len(disp_preds)
    assert n_predictions >= 1
    disp_loss = 0.0
    mag = torch.sum(disp_gt**2, dim=1).sqrt()
    valid = ((valid >= 0.5) & (mag < max_disp)).unsqueeze(1)
    assert valid.shape == disp_gt.shape, [valid.shape, disp_gt.shape]
    assert not torch.isinf(disp_gt[valid.bool()]).any()

    # quantile = torch.quantile((disp_init_pred - disp_gt).abs(), 0.9)
    init_valid = valid.bool() & ~torch.isnan(disp_init_pred)#  & ((disp_init_pred - disp_gt).abs() < quantile)
    disp_loss += 1.0 * F.smooth_l1_loss(disp_init_pred[init_valid], disp_gt[init_valid], reduction='mean')
    for i in range(n_predictions):
        adjusted_loss_gamma = loss_gamma**(15/(n_predictions - 1))
        i_weight = adjusted_loss_gamma**(n_predictions - i - 1)
        # print("disp_preds[i] shape", disp_preds[i].size(),disp_preds[i].max(), disp_preds[i].min() )
        # print("disp_gt shape", disp_gt.size(),disp_gt.max(), disp_gt.min() )
        i_loss = (disp_preds[i] - disp_gt).abs()
        # quantile = torch.quantile(i_loss, 0.9)
        assert i_loss.shape == valid.shape, [i_loss.shape, valid.shape, disp_gt.shape, disp_preds[i].shape]
        # print("loss is, ",i_loss[valid.bool() & ~torch.isnan(i_loss)].mean().item())
        disp_loss += i_weight * i_loss[valid.bool() & ~torch.isnan(i_loss)].mean()
        
    epe = torch.sum((disp_preds[-1] - disp_gt)**2, dim=1).sqrt()
    epe = epe.view(-1)[valid.view(-1)]

    if valid.bool().sum() == 0:
        epe = torch.Tensor([0.0]).cuda()

    metrics = {
        'train/epe': epe.mean(),
        'train/1px': (epe < 1).float().mean(),
        'train/3px': (epe < 3).float().mean(),
        'train/5px': (epe < 5).float().mean(),
    }
    return disp_loss, metrics

def fetch_optimizer(args, model):
    """ Create the optimizer and learning rate scheduler """
    DPT_params = list(map(id, model.feat_decoder.parameters())) 
    rest_params = filter(lambda x:id(x) not in DPT_params and x.requires_grad, model.parameters())

    params_dict = [{'params': model.feat_decoder.parameters(), 'lr': args.lr/2.0}, 
                   {'params': rest_params, 'lr': args.lr}, ]
    optimizer = optim.AdamW(params_dict, lr=args.lr, weight_decay=args.wdecay, eps=1e-8)

    scheduler = optim.lr_scheduler.OneCycleLR(optimizer, [args.lr/2.0, args.lr], args.total_step+100,
            pct_start=0.01, cycle_momentum=False, anneal_strategy='linear')


    return optimizer, scheduler

def criterion_uncertainty(u, la, alpha, beta, y, valid, weight_reg=0.05,max_disp=192):
    # our loss function
    disp_gt = y
    mag = torch.sum(disp_gt**2, dim=1).sqrt()
    valid = ((valid >= 0.5) & (mag < max_disp)).unsqueeze(1)
    assert valid.shape == disp_gt.shape, [valid.shape, disp_gt.shape]
    assert not torch.isinf(disp_gt[valid.bool()]).any()

    mask = valid.bool() & ~torch.isnan(u)#  & ((disp_init_pred - disp_gt).abs() < quantile)
    om = 2 * beta * (1 + la)
    om = torch.clamp(om, min=1e-10)
    # print("Om value is", om.item())
    # len(u): size
    if torch.sum(mask == True) == 0:
        unc_loss = torch.tensor(0.0, requires_grad=True)
    else:
        NLL_loss = 0.5 * torch.log(np.pi / la) - alpha * torch.log(om) + \
            (alpha + 0.5) * torch.log(la * (u - y) ** 2 + om) + \
                torch.lgamma(alpha) - torch.lgamma(alpha+0.5)
        unc_loss = NLL_loss[valid.bool() & ~torch.isnan(NLL_loss)].mean()
        R_loss = torch.abs(u - y) * (2 * la + alpha)
        lossr = weight_reg * R_loss[valid.bool() & ~torch.isnan(R_loss)].mean()
        unc_loss = unc_loss + lossr
    return unc_loss

def gradient_loss(pred, target):
    # 归一化到[0,1]
    pred_min, pred_max = pred.min(), pred.max()
    target_min, target_max = target.min(), target.max()
    
    pred = (pred - pred_min) / (pred_max - pred_min + 1e-8)
    target = (target - target_min) / (target_max - target_min + 1e-8)
    """计算梯度一致性损失"""
    # 计算深度梯度
    pred_dx = torch.abs(pred[:, :, :, :-1] - pred[:, :, :, 1:])
    pred_dy = torch.abs(pred[:, :, :-1, :] - pred[:, :, 1:, :])
    
    target_dx = torch.abs(target[:, :, :, :-1] - target[:, :, :, 1:])
    target_dy = torch.abs(target[:, :, :-1, :] - target[:, :, 1:, :])
    
    # 比较梯度
    grad_diff_x = torch.abs(pred_dx - target_dx)
    grad_diff_y = torch.abs(pred_dy - target_dy)
    
    return torch.mean(grad_diff_x) + torch.mean(grad_diff_y)


def disparity_uncertainty_convergence_ranking_loss(uncertainty_convergence, disparity_convergence, valid):
    valid = (valid >= 0.5).unsqueeze(1)
    if len(disparity_convergence.shape) == 4:
        error = rearrange(disparity_convergence, 'b c h w -> b (c h w)')
        uncertainty = rearrange(uncertainty_convergence, 'b c h w -> b (c h w)')
        valid = rearrange(valid, 'b c h w -> b (c h w)')
    randperm_idx = torch.randperm(error.shape[-1])
    error_ = error.clone()[..., randperm_idx]
    uncertainty_ = uncertainty.clone()[..., randperm_idx]
    valid = valid[..., randperm_idx]

    ranking_loss = ((error-error_).detach() - (uncertainty-uncertainty_)).clamp(0)
    ranking_loss = ranking_loss[valid.bool() & ~torch.isnan(ranking_loss)].mean()
    return ranking_loss

@hydra.main(version_base=None, config_path='config', config_name='train_HILC_sceneflow_v0')
def main(cfg):
    set_seed(cfg.seed)
    logger = get_logger(__name__)
    Path(cfg.save_path).mkdir(exist_ok=True, parents=True)
    kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(mixed_precision='fp16', dataloader_config=DataLoaderConfiguration(use_seedable_sampler=True), log_with='wandb', kwargs_handlers=[kwargs], step_scheduler_with_optimizer=False)
    accelerator.init_trackers(project_name=cfg.project_name, config=OmegaConf.to_container(cfg, resolve=True), init_kwargs={'wandb': cfg.wandb})

    train_dataset = datasets.fetch_dataloader(cfg)
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=cfg.batch_size//cfg.num_gpu,
        pin_memory=True, shuffle=True, num_workers=int(4), drop_last=True)

    aug_params = {}
    val_dataset = datasets.SceneFlowDatasets(dstype='frames_finalpass', things_test=True)
    val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=8, pin_memory=True, shuffle=False, num_workers=8, drop_last=False)

    model = Monster(cfg)
    if not cfg.restore_ckpt.endswith("None"):
        assert cfg.restore_ckpt.endswith(".pth")
        print(f"Loading checkpoint from {cfg.restore_ckpt}")
        assert os.path.exists(cfg.restore_ckpt)
        checkpoint = torch.load(cfg.restore_ckpt, map_location='cpu')
        ckpt = dict()
        if 'state_dict' in checkpoint.keys():
            checkpoint = checkpoint['state_dict']
        for key in checkpoint:
            # if 'REMP.final_conv' in key:
            #     continue
            ckpt[key.replace('module.', '')] = checkpoint[key]

        model.load_state_dict(ckpt, strict=False)
        print(f"Loaded checkpoint from {cfg.restore_ckpt} successfully")
        del ckpt, checkpoint
    optimizer, lr_scheduler = fetch_optimizer(cfg, model)
    train_loader, model, optimizer, lr_scheduler, val_loader = accelerator.prepare(train_loader, model, optimizer, lr_scheduler, val_loader)
    model.to(accelerator.device)

    total_step = 0
    should_keep_training = True
    while should_keep_training:
        active_train_loader = train_loader

        model.train()
        model.module.freeze_bn()
        i = 0
        for data in tqdm(active_train_loader, dynamic_ncols=True, disable=not accelerator.is_main_process):
            i = i + 1
            _, left, right, disp_gt, valid = [x for x in data]

            # first optimization
            with accelerator.autocast():
                disp_init_pred, disp_preds, depth_mono, v, alpha, beta = model(left, right, iters=cfg.train_iters)
                epistemic = beta / (alpha - 1) / v
            loss, metrics = sequence_loss(disp_preds, disp_init_pred, disp_gt, valid, max_disp=cfg.max_disp)
            uncertainty_loss = criterion_uncertainty(disp_preds[-1], v, alpha, beta, disp_gt, valid)
            loss = loss + uncertainty_loss
            accelerator.backward(loss)
            accelerator.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

            total_step += 1
            loss = accelerator.reduce(loss.detach(), reduction='mean')
            metrics = accelerator.reduce(metrics, reduction='mean')
            accelerator.log({'train/loss': loss, 'train/learning_rate': optimizer.param_groups[0]['lr']}, total_step)
            accelerator.log(metrics, total_step)

            first_disparity = disp_preds[-1].detach()
            first_epistemic = epistemic.detach()

            # Second optimization
            with accelerator.autocast():
                disp_init_pred, disp_preds, depth_mono, v, alpha, beta = model(left, right, iters=cfg.train_iters)
                epistemic = beta / (alpha - 1) / v
                
            loss, metrics = sequence_loss(disp_preds, disp_init_pred, disp_gt, valid, max_disp=cfg.max_disp)
            uncertainty_loss = criterion_uncertainty(disp_preds[-1], v, alpha, beta, disp_gt, valid)
            # uncertainty_loss = 0 
            if i % 20 == 0:
                print("Loss is {}, and uncertainty loss is {}".format(loss.item(), uncertainty_loss.item()))

            training_consistency = torch.abs(disp_preds[-1] - first_disparity) / (first_disparity + 0.000001)
            epistemic_consistency = torch.abs(epistemic - first_epistemic) / (first_epistemic + 0.000001)

            

            consistency_loss = torch.nn.functional.relu(-torch.sign(epistemic - first_epistemic) * training_consistency + epistemic_consistency)
            consistency_loss = consistency_loss.mean()

            intra_consistency_loss = disparity_uncertainty_convergence_ranking_loss(epistemic_consistency, training_consistency, valid)
            if i % 20 == 0:
                print("consistency_loss", consistency_loss.item(), "intra_consistency_loss", intra_consistency_loss.item())
            loss = loss + uncertainty_loss + 0.1 * consistency_loss + 0.1 * intra_consistency_loss
            accelerator.backward(loss)
            accelerator.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

            total_step += 1
            loss = accelerator.reduce(loss.detach(), reduction='mean')
            metrics = accelerator.reduce(metrics, reduction='mean')
            accelerator.log({'train/loss': loss, 'train/learning_rate': optimizer.param_groups[0]['lr']}, total_step)
            accelerator.log(metrics, total_step)


            ####visualize the depth_mono and disp_preds
            if total_step % 20 == 0 and accelerator.is_main_process:
                image1_np = left[0].squeeze().cpu().numpy()
                image1_np = (image1_np - image1_np.min()) / (image1_np.max() - image1_np.min()) * 255.0
                image1_np = image1_np.astype(np.uint8)
                image1_np = np.transpose(image1_np, (1, 2, 0))

                image2_np = right[0].squeeze().cpu().numpy()
                image2_np = (image2_np - image2_np.min()) / (image2_np.max() - image2_np.min()) * 255.0
                image2_np = image2_np.astype(np.uint8)
                image2_np = np.transpose(image2_np, (1, 2, 0))


                depth_mono_np = gray_2_colormap_np(depth_mono[0].squeeze())
                disp_preds_np = gray_2_colormap_np(disp_preds[-1][0].squeeze())
                disp_gt_np = gray_2_colormap_np(disp_gt[0].squeeze())
                
                accelerator.log({"disp_pred": wandb.Image(disp_preds_np, caption="step:{}".format(total_step))}, total_step)
                accelerator.log({"disp_gt": wandb.Image(disp_gt_np, caption="step:{}".format(total_step))}, total_step)
                accelerator.log({"depth_mono": wandb.Image(depth_mono_np, caption="step:{}".format(total_step))}, total_step)

            if (total_step > 0) and (total_step % cfg.save_frequency == 0):
                if accelerator.is_main_process:
                    save_path = Path(cfg.save_path + '/%d.pth' % (total_step))
                    model_save = accelerator.unwrap_model(model)
                    checkpoint_all = {
                        "state_dict": model_save.state_dict(),
                        'optimizer': optimizer.state_dict(),
                        'lr_scheduler': lr_scheduler.state_dict(),
                    }
                    torch.save(checkpoint_all, save_path)
                    del model_save
        
            if (total_step > 0) and (total_step % cfg.val_frequency == 0):

                model.eval()
                elem_num, total_epe, total_out = 0, 0, 0
                for data in tqdm(val_loader, dynamic_ncols=True, disable=not accelerator.is_main_process):
                    _, left, right, disp_gt, valid = [x for x in data]
                    padder = InputPadder(left.shape, divis_by=32)
                    left, right = padder.pad(left, right)
                    with torch.no_grad():
                        disp_pred, v, alpha, beta = model(left, right, iters=cfg.valid_iters, test_mode=True)
                    disp_pred = padder.unpad(disp_pred)
                    assert disp_pred.shape == disp_gt.shape, (disp_pred.shape, disp_gt.shape)
                    epe = torch.abs(disp_pred - disp_gt)
                    out = (epe > 1.0).float()
                    epe = torch.squeeze(epe, dim=1)
                    out = torch.squeeze(out, dim=1)
                    disp_gt = torch.squeeze(disp_gt, dim=1)
                    epe, out = accelerator.gather_for_metrics((epe[(valid >= 0.5) & (disp_gt.abs() < 192)].mean(), out[(valid >= 0.5) & (disp_gt.abs() < 192)].mean()))
                    elem_num += epe.shape[0]
                    for i in range(epe.shape[0]):
                        total_epe += epe[i]
                        total_out += out[i]
                    accelerator.log({'val/epe': total_epe / elem_num, 'val/d1': 100 * total_out / elem_num}, total_step)

                model.train()
                model.module.freeze_bn()

            if total_step == cfg.total_step:
                should_keep_training = False
                break

    if accelerator.is_main_process:
        save_path = Path(cfg.save_path + '/final.pth')
        model_save = accelerator.unwrap_model(model)
        torch.save(model_save.state_dict(), save_path)
        del model_save
    
    accelerator.end_training()

if __name__ == '__main__':
    main()