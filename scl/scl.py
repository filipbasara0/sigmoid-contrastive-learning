import torch
import copy
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from scl.utils import get_feature_size


class MLPHead(torch.nn.Module):

    def __init__(self, in_dim, out_dim, hidden_dim=512):
        super(MLPHead, self).__init__()
        self.block = torch.nn.Sequential(
            torch.nn.Linear(in_dim, hidden_dim, bias=False),
            nn.BatchNorm1d(hidden_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(hidden_dim, out_dim, bias=False)
        )

    def forward(self, x):
        return self.block(x)

def scl_loss(x, x_prime, tau, b, alpha=0.0, max_tau=5.0,
             gamma=0.0, global_step=-1.0, penalty_filtering=False,
             filtering_warmup_steps=5000, filtering_threshold=0.05):
    """
    Parameters:
    x (torch.Tensor): Online projections [n, dim].
    x_prime (torch.Tensor): Target projections of shape [n, dim].
    tau (torch.Tensor): Learnable temperature parameter.
    b (torch.Tensor): Learnable bias parameter.
    alpha (float): KL divergence (regularization term) weight.
    global_step (float): Global training step.
    penalty_filtering (boolean): If True, explicitly exclude pairs that are below a threshold.
    filtering_warmup_steps (float): Warm up with standard confidence penalty (no explicit filtering) for this many steps.
    filtering_threshold (float): Keep only pairs that have a confidence penalty above this threshold.
    """
    n = x.size(0)
    labels = 2 * torch.eye(n, device=x.device) - 1
    
    x, x_prime = F.normalize(x, p=2, dim=-1), F.normalize(x_prime, p=2, dim=-1)
    logits = torch.mm(x, x_prime.t()) * tau.exp().clamp(0, max_tau) + b
    
    probs = torch.sigmoid(labels * logits)
    conf_penalty = (1 - probs) ** gamma
    
    if penalty_filtering and global_step > filtering_warmup_steps:
        # Create a mask to include all positive pairs and selected negative pairs
        positive_mask = torch.eye(n, device=x.device).bool()
        negative_mask = (~positive_mask) & (conf_penalty > filtering_threshold)
        selection_mask = positive_mask | negative_mask
        # Apply the mask to logits and labels
        selected_logits = logits[selection_mask]
        selected_labels = labels[selection_mask]
        # Compute the loss using only the selected pairs
        loss = -torch.sum(F.logsigmoid(selected_labels * selected_logits)) / n
    else:
        loss = -torch.sum(conf_penalty * F.logsigmoid(labels * logits)) / n

    # KL divergence loss
    p1 = torch.nn.functional.log_softmax(logits, dim=1)
    p2 = torch.nn.functional.softmax(logits, dim=0).t()
    invariance_loss = torch.nn.functional.kl_div(p1, p2, reduction="batchmean")

    loss = loss + alpha * invariance_loss

    # return invariance_loss for debug
    return loss, invariance_loss


class ReLIC(torch.nn.Module):

    def __init__(self,
                 encoder,
                 mlp_out_dim=64,
                 mlp_hidden=512,
                 mlp_in_dim=None,
                 init_tau=np.log(1.0),
                 init_b=0):
        super(ReLIC, self).__init__()

        if not mlp_in_dim:
            mlp_in_dim = get_feature_size(encoder)
        critic = MLPHead(mlp_in_dim, mlp_out_dim, mlp_hidden)
        self.online_encoder = torch.nn.Sequential(encoder, critic)

        self.target_encoder = copy.deepcopy(self.online_encoder)
        self.target_encoder.requires_grad_(False)

        self.tau = nn.Parameter(torch.ones([]) * init_tau)
        self.b = nn.Parameter(torch.ones([]) * init_b)

    @torch.inference_mode()
    def get_features(self, img):
        with torch.no_grad():
            return self.target_encoder[0](img)

    def forward(self, x1, x2):
        o1, o2 = self.online_encoder(x1), self.online_encoder(x2)
        with torch.no_grad():
            t1, t2 = self.target_encoder(x1), self.target_encoder(x2)
        t1, t2 = t1.detach(), t2.detach()
        return o1, o2, t1, t2
    
    @torch.inference_mode()
    def get_target_pred(self, x):
        with torch.no_grad():
            t = self.target_encoder(x)
        t = t.detach()
        return t
    
    def get_online_pred(self, x):
        return self.online_encoder(x)

    def update_params(self, gamma):
        with torch.no_grad():
            valid_types = [torch.float, torch.float16]
            for o_param, t_param in self._get_params():
                if o_param.dtype in valid_types and t_param.dtype in valid_types:
                    t_param.data.lerp_(o_param.data, 1. - gamma)

            for o_buffer, t_buffer in self._get_buffers():
                if o_buffer.dtype in valid_types and t_buffer.dtype in valid_types:
                    t_buffer.data.lerp_(o_buffer.data, 1. - gamma)

    def copy_params(self):
        for o_param, t_param in self._get_params():
            t_param.data.copy_(o_param)

        for o_buffer, t_buffer in self._get_buffers():
            t_buffer.data.copy_(o_buffer)

    def save_encoder(self, path):
        torch.save(self.target_encoder[0].state_dict(), path)

    def _get_params(self):
        return zip(self.online_encoder.parameters(),
                   self.target_encoder.parameters())

    def _get_buffers(self):
        return zip(self.online_encoder.buffers(),
                   self.target_encoder.buffers())
