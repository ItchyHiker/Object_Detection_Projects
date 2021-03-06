from easydict import EasyDict
import torch
import torch.nn as nn
import torch.nn.functional as F

from tools.box_utils import match, log_sum_exp, match_atss


class FocalLoss(nn.Module):
    def __init__(self, alpha=0.25, gamma=2, num_classes=2, reduction='sum'):
        """Focal Loss from: https://arxiv.org/abs/1708.02002
        alpha: background weight, alpha for background, 1-alpha for foreground objects.
        gamma: parameter for adjusting the relative importance of easy and hard samples, larger gamma increase the
            relative importance of hard samples vs easy samples.
        num_classes: number of classes, in this case, we only have face, non-face.
        reduction: loss accumulation type in a batch.
        """
        super(FocalLoss, self).__init__()
        self.reduction = reduction
        self.alpha = alpha
        self.gamma = gamma

    def forward(self, pred_logits, targets):
        preds = pred_logits.sigmoid()
        ce = F.binary_cross_entropy_with_logits(pred_logits, targets, reduction=self.reduction)
        alpha = targets * self.alpha + (1. - targets) * (1. - self.alpha)
        pt = torch.where(targets == 1,  preds, 1 - preds)
        loss = alpha * (1. - pt) ** self.gamma * ce

        '''
        pos_boxes_mask = targets == 1
        num_pos = pos_boxes_mask.long().sum(1, keepdim=True)
        N = max(num_pos.sum().float(), 1)
        loss = loss.sum() / N
        '''
        return loss.mean()


class OHEM(nn.Module):
    def __init__(self, neg_pos_ratio, num_classes):
        super(OHEM, self).__init__()
        self.neg_pos_ratio = neg_pos_ratio
        self.num_classes = num_classes

    def forward(self, pred_logits, targets):
        batch_size = pred_logits.shape[0]
        pos_boxes_mask = targets == 1

        # Online Hard Negative Mining, keep balance of nums of positive and negative samples
        loss_c = F.cross_entropy(pred_logits.view(-1, self.num_classes), targets.view(-1), reduce=False)
        loss_c = loss_c.unsqueeze(-1)
        loss_c[pos_boxes_mask.view(-1, 1)] = 0  # filter out positive boxes and only keep negative boxes
        loss_c = loss_c.view(batch_size, -1)
        _, loss_idx = loss_c.sort(1, descending=True)
        _, idx_rank = loss_idx.sort(1)
        num_pos = pos_boxes_mask.long().sum(1, keepdim=True)
        num_neg = torch.clamp(self.neg_pos_ratio * num_pos, max=pos_boxes_mask.size(1) - 1)
        neg_boxes_mask = idx_rank < num_neg.expand_as(idx_rank)

        # Classification cross entropy loss with balanced positive and negative loss
        pos_boxes_idx = pos_boxes_mask.unsqueeze(2).expand_as(pred_logits)
        neg_boxes_idx = neg_boxes_mask.unsqueeze(2).expand_as(pred_logits)
        cls_pred = pred_logits[(pos_boxes_idx + neg_boxes_idx).gt(0)].view(-1, self.num_classes)
        cls_gt = targets[(pos_boxes_mask + neg_boxes_mask).gt(0)]
        cls_loss = F.cross_entropy(cls_pred, cls_gt, reduction='sum')

        N = max(num_pos.sum().float(), 1)
        cls_loss /= N

        return cls_loss


class MultiBoxLoss(nn.Module):
    """SSD Weighted Loss Function
    Compute Targets:
        1) Produce Confidence Target Indices by matching  ground truth boxes
           with (default) 'priorboxes' that have jaccard index > threshold parameter
           (default threshold: 0.5).
        2) Produce localization target by 'encoding' variance into offsets of ground
           truth boxes and their matched  'priorboxes'.
        3) Hard negative mining to filter the excessive number of negative examples
           that comes with using a large number of default bounding boxes.
           (default negative:positive ratio 3:1)
    Objective Loss:
        L(x,c,l,g) = (Lconf(x, c) + αLloc(x,l,g)) / N
        Where, Lconf is the CrossEntropy Loss and Lloc is the SmoothL1 Loss
        weighted by α which is set to 1 by cross val.
        Args:
            c: class confidences,
            l: predicted boxes,
            g: ground truth boxes
            N: number of matched default boxes
        See: https://arxiv.org/pdf/1512.02325.pdf for more details.
    """

    def __init__(self, cfg: EasyDict):
        super(MultiBoxLoss, self).__init__()
        self.num_classes = cfg.MODEL.num_classes
        self.thresholds = cfg.TRAIN.overlap_thresholds
        self.cls_loss_type = cfg.TRAIN.cls_loss_type
        self.neg_pos_ratio = cfg.TRAIN.neg_pos_ratio
        self.variance = cfg.TRAIN.encode_variance
        self.use_gpu = cfg.TRAIN.use_gpu
        self.focal_loss = FocalLoss()
        self.ohem = OHEM(cfg.TRAIN.neg_pos_ratio, cfg.MODEL.num_classes)
        self.cfg = cfg

    def forward(self, predictions, anchor_boxes, targets):
        """
        Args:
            predictions (tuple): A tuple containing loc preds, conf preds,
                and landmark preds from SSD net.
                conf shape: torch.size(batch_size, num_anchors, num_classes)
                loc shape: torch.size(batch_size, num_anchors, 4)
                priors shape: torch.size(num_priors,4)
            anchor_boxes (tensor): torch.size(num_anchors, 4), prior box generated
                from PriorBox
            targets (tensor): Ground truth boxes, landmarks and label for a batch,
                shape: [batch_size,num_objs, 4+1+2*5] (last idx is the label).
        """

        # matched_labels, matched_boxes, matched_landmarks = match(self.thresholds, targets, anchor_boxes, self.variance)
        matched_labels, matched_boxes, matched_landmarks = match_atss(self.cfg, targets, anchor_boxes, self.variance)
        pred_logits, pred_boxes, pred_landmarks = predictions
        batch_size = pred_boxes.size(0)

        # 1. calculate landmarks loss for anchors which are above IoU threshold with ground truth boxes
        zeros = torch.tensor(0).cuda()
        pos_ldmks_mask = matched_labels > zeros  # when there landmark information is not usable, face label will be -1
        num_pos_ldmks = pos_ldmks_mask.long().sum(1, keepdim=True)
        N1 = max(num_pos_ldmks.sum().float(), 1)
        pos_ldmks_idx = pos_ldmks_mask.unsqueeze(pos_ldmks_mask.dim()).expand_as(pred_landmarks)
        pos_ldmks_pred = pred_landmarks[pos_ldmks_idx].view(-1, 10)
        pos_ldmks_gt = matched_landmarks[pos_ldmks_idx].view(-1, 10)
        # Landmark regression smooth l1 loss, shape: [batch_size, num_prior_boxes, 10]
        landmark_loss = F.smooth_l1_loss(pos_ldmks_pred, pos_ldmks_gt, reduction='sum')
        landmark_loss /= N1

        # 2. calculate box loss for anchors which are above IoU threshold with ground truth boxes
        pos_boxes_mask = matched_labels != zeros  # when there landmark information is not usable, face label will be -1
        matched_labels[pos_boxes_mask] = 1  # convert all -1 label faces into 1
        pos_boxes_idx = pos_boxes_mask.unsqueeze(pos_boxes_mask.dim()).expand_as(pred_boxes)
        pos_boxes_pred = pred_boxes[pos_boxes_idx].view(-1, 4)
        pos_boxes_gt = matched_boxes[pos_boxes_idx].view(-1, 4)
        # Box regression smooth l1 loss, shape: [batch_size, num_prior_boxes, 4]
        box_loss = F.smooth_l1_loss(pos_boxes_pred, pos_boxes_gt, reduction='sum')
        num_pos = pos_boxes_mask.long().sum(1, keepdim=True)
        N = max(num_pos.sum().float(), 1)
        box_loss /= N

        '''
        # 3. Online Hard Negative Mining, keep balance of nums of positive and negative samples
        loss_c = F.cross_entropy(pred_logits.view(-1, self.num_classes), matched_labels.view(-1), reduce=False)
        loss_c = loss_c.unsqueeze(-1)
        loss_c[pos_boxes_mask.view(-1, 1)] = 0  # filter out positive boxes and only keep negative boxes
        loss_c = loss_c.view(batch_size, -1)
        _, loss_idx = loss_c.sort(1, descending=True)
        _, idx_rank = loss_idx.sort(1)
        num_pos = pos_boxes_mask.long().sum(1, keepdim=True)
        num_neg = torch.clamp(self.neg_pos_ratio*num_pos, max=pos_boxes_mask.size(1)-1)
        neg_boxes_mask = idx_rank < num_neg.expand_as(idx_rank)

        # 4. Classification cross entropy loss with balanced positive and negative loss
        pos_boxes_idx = pos_boxes_mask.unsqueeze(2).expand_as(pred_logits)
        neg_boxes_idx = neg_boxes_mask.unsqueeze(2).expand_as(pred_logits)
        cls_pred = pred_logits[(pos_boxes_idx+neg_boxes_idx).gt(0)].view(-1, self.num_classes)
        cls_gt = matched_labels[(pos_boxes_mask+neg_boxes_mask).gt(0)]
        cls_loss = F.cross_entropy(cls_pred, cls_gt, reduction='sum')
        '''

        if self.cls_loss_type == "OHEM":
            cls_loss = self.ohem(pred_logits, matched_labels)

        if self.cls_loss_type == "FocalLoss":
            matched_labels_target = F.one_hot(matched_labels.view(-1))
            cls_loss = self.focal_loss(pred_logits.view(-1, self.num_classes), matched_labels_target.to(pred_logits.dtype))
            # cls_loss *= 10

        # Sum of losses: L(x,c,l,g) = (Lcls(x, c) + αLloc(x,l,g)) / N

        return cls_loss, box_loss, landmark_loss
