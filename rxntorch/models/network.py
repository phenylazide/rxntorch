from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from sampler import BinRandomSampler
from rxntorch.layers import GraphConvolution


class GCN(nn.Module):
    def __init__(self, dataset, batch_size, pre_proc_threads=1):
        super(GCN, self).__init__()
        self.dataset = dataset
        self.batch_size = batch_size
        self.sampler = BinRandomSampler(self.dataset.bins, self.batch_size)
        self.data_loader = DataLoader(self.dataset,
                                    batch_size=self.batch_size,
                                    num_workers=pre_proc_threads,
                                    sampler=self.sampler)
        self.gc1 = GraphConvolution()
        self.gc2 = GraphConvolution()
        self.gc3 = GraphConvolution()

    def forward(self, x, adj):

class Transformer(nn.module):
    def __init__(self, dataset, batch_size, pre_proc_threads=1):
        super(Transformer, self).__init__()
        self.dataset = dataset
        self.batch_size = batch_size
        self.sampler = BinRandomSampler(self.dataset.bins, self.batch_size)
        self.data_loader = DataLoader(self.dataset,
                                    batch_size=self.batch_size,
                                    num_workers=pre_proc_threads,
                                    sampler=self.sampler)

    def forward(self, x, adj):
