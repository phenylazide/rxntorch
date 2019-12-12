import logging

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adam

from .wln import WLNet
from .attention import Attention
from .reactivity_scoring import ReactivityScoring


class ReactivityNet(nn.Module):
    def __init__(self, depth, afeats_size, bfeats_size, hidden_size, binary_size):
        super(ReactivityNet, self).__init__()
        self.hidden_size = hidden_size
        self.wln = WLNet(depth, afeats_size, bfeats_size, hidden_size)
        self.attention = Attention(hidden_size, binary_size)
        self.reactivity_scoring = ReactivityScoring(hidden_size, binary_size)

    def forward(self, fatoms, fbonds, atom_nb, bond_nb, num_nbs, n_atoms, binary_feats, blabels, mask_neis, mask_atoms):
        local_features = self.wln(fatoms, fbonds, atom_nb, bond_nb, num_nbs, n_atoms, mask_neis, mask_atoms)
        local_pair, global_pair = self.attention(local_features, binary_feats)
        pair_scores = self.reactivity_scoring(local_pair, global_pair, binary_feats)
        masked_scores = torch.where((blabels == -1.0), pair_scores - 10000, pair_scores)
        _, top_k = torch.topk(torch.flatten(masked_scores, start_dim=1, end_dim=-1), 20)
        return pair_scores, top_k


class ReactivityTrainer(nn.Module):
    def __init__(self, rxn_net, train_dataloader, test_dataloader, lr=1e-4, betas=(0.9, 0.999), weight_decay=0.01,
                 with_cuda=True, cuda_devices=None, log_freq=10, grad_clip=None):
        super(ReactivityTrainer, self).__init__()
        cuda_condition = torch.cuda.is_available() and with_cuda
        self.device = torch.device("cuda:0" if cuda_condition else "cpu")
        self.model = rxn_net
        if with_cuda and torch.cuda.device_count() > 1:
            logging.info("Using {} GPUS".format(torch.cuda.device_count()))
            self.model = nn.DataParallel(self.model, device_ids=cuda_devices)
        self.train_data = train_dataloader
        self.test_data = test_dataloader
        self.lr = lr
        self.grad_clip = grad_clip
        self.optimizer = Adam(self.model.parameters(), lr=self.lr, betas=betas, weight_decay=weight_decay)
        self.log_freq = log_freq
        self.model.to(self.device)
        logging.info("Total Parameters: {:,d}".format(sum([p.nelement() for p in self.model.parameters()])))

    def train(self, epoch):
        self.iterate(epoch, self.train_data)

    def test(self, epoch):
        self.iterate(epoch, self.test_data, train=False)

    def iterate(self, epoch, data_loader, train=True):
        avg_loss = 0.0
        sum_acc_10, sum_acc_20, sum_gnorm = 0.0, 0.0, 0.0
        iters = len(data_loader)

        for i, data in enumerate(data_loader):
            data = {key: value.to(self.device) for key, value in data.items()}

            #Create some masking logic for padding
            mask_neis = torch.unsqueeze(
                data['n_bonds'].unsqueeze(-1) > torch.arange(0, 10, dtype=torch.int32, device=self.device).view(1, 1, -1), -1)
            max_n_atoms = data['n_atoms'].max()
            mask_atoms = torch.unsqueeze(
                data['n_atoms'].unsqueeze(-1) > torch.arange(0, max_n_atoms, dtype=torch.int32, device=self.device).view(1, -1),
                -1)

            pair_scores, top_k = self.model.forward(data['atom_features'], data['bond_features'], data['atom_graph'],
                                                    data['bond_graph'], data['n_bonds'], data['n_atoms'],
                                                    data['binary_features'], data['bond_labels'], mask_neis, mask_atoms)
            bond_labels = F.relu(data['bond_labels'])
            loss = F.binary_cross_entropy_with_logits(pair_scores, bond_labels, reduction='none')
            loss *= torch.ne(data['bond_labels'], -1).float()
            loss = torch.mean(loss)
            avg_loss += loss.item()

            # 3. backward and optimization only in train
            if train:
                self.optimizer.zero_grad()
                loss.backward()
                param_norm = torch.sqrt(sum([torch.sum(param ** 2) for param in self.model.parameters()])).item()
                grad_norm = torch.sqrt(sum([torch.sum(param.grad ** 2) for param in self.model.parameters()])).item()
                sum_gnorm += grad_norm
                if self.grad_clip is not None:
                    nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_clip)
                self.optimizer.step()
            # Gather the indices of bond labels where a bond changes to calculate accuracy
            sp_labels = torch.stack(torch.where(torch.flatten(data['bond_labels'],
                                                              start_dim=1, end_dim=-1) == 1), dim=-1)

            batch_size, nk = top_k.shape[0], top_k.shape[1]
            sp_top_k = torch.empty((batch_size * nk, 2), dtype=torch.int64, device=self.device)
            for j in range(batch_size):
                for k in range(nk):
                    sp_top_k[j * nk + k, 0], sp_top_k[j * nk + k, 1] = j, top_k[j, k]
            mol_label_idx = [torch.where(sp_labels[:,0] == i)[0] for i in range(batch_size)]
            mol_topk_idx = [torch.where(sp_top_k[:,0] == i)[0] for i in range(batch_size)]
            mol_labels = [sp_labels[idx,1] for idx in mol_label_idx]
            mol_topk = [sp_top_k[idx,1] for idx in mol_topk_idx]
            hits_10 = [(mol_labels[i].unsqueeze(0) == mol_topk[i][:10].unsqueeze(1)).any(dim=0) for i in range(batch_size)]
            hits_20 = [(mol_labels[i].unsqueeze(0) == mol_topk[i].unsqueeze(1)).any(dim=0) for i in range(batch_size)]

            all_correct_10 = [mol_hits.all().int() for mol_hits in hits_10]
            all_correct_20 = [mol_hits.all().int() for mol_hits in hits_20]
            sum_acc_10 += sum(all_correct_10).item()
            sum_acc_20 += sum(all_correct_20).item()

            if (i+1) % self.log_freq == 0:
                if train:
                    post_fix = {
                        "epoch": epoch,
                        "iter": (i+1),
                        "iters": iters,
                        "avg_loss": avg_loss,
                        "acc10": sum_acc_10 / (self.log_freq * batch_size),
                        "acc20": sum_acc_20 / (self.log_freq * batch_size),
                        "pnorm": param_norm,
                        "gnorm": sum_gnorm
                    }
                    logging.info(("Epoch: {epoch:2d}  Iteration: {iter:6,d}/{iters:,d}  Average loss: {avg_loss:f}  "
                        "Accuracy @10: {acc10:6.2%}  @20: {acc20:6.2%}  Param norm: {pnorm:8.4f}  "
                        "Grad norm: {gnorm:8.4f}").format(
                        **post_fix))
                else:
                    post_fix = {
                        "epoch": epoch,
                        "iter": (i + 1),
                        "iters": iters,
                        "avg_loss": avg_loss,
                        "acc10": sum_acc_10 / (self.log_freq * batch_size),
                        "acc20": sum_acc_20 / (self.log_freq * batch_size)
                    }
                    logging.info(("Epoch: {epoch:2d}  Iteration: {iter:6,d}/{iters:,d}  Average loss: {avg_loss:f}  "
                        "Accuracy @10: {acc10:6.2%}  @20: {acc20:6.2%}").format(
                        **post_fix))
                sum_acc_10, sum_acc_20, sum_gnorm = 0.0, 0.0, 0.0
                avg_loss = 0.0

            if (i+1) % 1 == 0:
                for param_group in self.optimizer.param_groups:
                    param_group['lr'] = self.lr * 0.9

    def save(self, epoch, file_path="output/trained.model"):
        """
        Saving the current BERT model on file_path
        :param epoch: current epoch number
        :param file_path: model output path which gonna be file_path+"ep%d" % epoch
        :return: final_output_path
        """
        output_path = file_path + ".ep%d" % epoch
        torch.save(self.model.cpu(), output_path)
        self.model.to(self.device)
        print("EP:%d Model Saved on:" % epoch, output_path)
        return output_path
