import torch
from itertools import product as product
import numpy as np
from math import ceil


class AnchorGenerator(object):
    """Anchor generator"""
    def __init__(self, cfg):
        super(AnchorGenerator, self).__init__()
        self.min_sizes = cfg.MODEL.anchor_sizes
        self.steps = cfg.MODEL.strides
        self.clip = cfg.TRAIN.clip_box
        self.image_size = cfg.DATA.image_size
        self.feature_maps = [[ceil(self.image_size[0]/step), ceil(self.image_size[1]/step)] for step in self.steps]
        self.name = "s"
        self.use_gpu = cfg.TRAIN.use_gpu

    def generate_anchors(self):
        anchors = []
        for k, f in enumerate(self.feature_maps):
            min_sizes = self.min_sizes[k]
            for i, j in product(range(f[0]), range(f[1])):  # each feature map cell
                for min_size in min_sizes:  # anchor size for each cell
                    # anchor size in original image normalized by its size
                    s_kx = min_size / self.image_size[1]
                    s_ky = min_size / self.image_size[0]
                    # anchor center in original image normalized by its size
                    dense_cx = [x * self.steps[k] / self.image_size[1] for x in [j + 0.5]]
                    dense_cy = [y * self.steps[k] / self.image_size[0] for y in [i + 0.5]]
                    for cy, cx in product(dense_cy, dense_cx):  # mesh grid
                        anchors += [cx, cy, s_kx, s_ky]

        output = torch.Tensor(anchors).view(-1, 4)
        if self.clip:
            output.clamp_(max=1, min=0)
        if self.use_gpu:
            output = output.cuda()

        return output
