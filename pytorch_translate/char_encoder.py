#!/usr/bin/env python3

import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F

from pytorch_translate import common_layers  # noqa
from pytorch_translate import rnn

class HighwayLayer(nn.Module):

    def __init__(
        self,
        input_dim,
        transform_activation=F.relu,
        gate_activation=F.softmax,
        # Srivastava et al. (2015) recommend initializing bT to a negative
        # value, in order to militate the initial behavior towards carry.
        # We initialized bT to a small interval around −2
        gate_bias=-2,
    ):
        super().__init__()
        self.highway_transform_activation = transform_activation
        self.highway_gate_activation = gate_activation
        self.highway_transform = nn.Linear(input_dim, input_dim)
        self.highway_gate = nn.Linear(input_dim, input_dim)
        self.highway_gate.bias.data.fill_(gate_bias)

    def forward(self, x):
        transform_output = self.highway_transform_activation(self.highway_transform(x))
        gate_output = self.highway_gate_activation(self.highway_gate(x))

        transformation_part = torch.mul(transform_output, gate_output)
        carry_part = torch.mul((1 - gate_output), x)
        return torch.add(transformation_part, carry_part)


class CharCNNModel(nn.Module):
    """
    A Conv network to generate word embedding from character embeddings, from
    Character-Aware Neural Language Models, https://arxiv.org/abs/1508.06615.

    Components include convolutional filters, max pooling, and
    optional highway network.
    """
    def __init__(
        self,
        dictionary,
        num_chars=50,
        char_embed_dim=32,
        convolutions_params='((128, 3), (128, 5))',
        nonlinear_fn_type='tanh',
        num_highway_layers=0,
    ):
        super().__init__()
        self.dictionary = dictionary
        self.padding_idx = dictionary.pad()
        self.convolutions_params = convolutions_params
        self.num_highway_layers = num_highway_layers

        if nonlinear_fn_type == "tanh":
            nonlinear_fn = nn.Tanh
        elif nonlinear_fn_type == "relu":
            nonlinear_fn = nn.ReLU
        else:
            raise Exception(
                "Invalid nonlinear type: {}".format(nonlinear_fn_type)
            )

        self.embed_chars = rnn.Embedding(
            num_embeddings=num_chars,
            embedding_dim=char_embed_dim,
            padding_idx=self.padding_idx,
            freeze_embed=False,
        )
        self.convolutions = nn.ModuleList([
            nn.Sequential(
                nn.Conv1d(
                    char_embed_dim,
                    num_filters,
                    kernel_size,
                    padding=kernel_size,
                ),
                nonlinear_fn()
            ) for (num_filters, kernel_size) in self.convolutions_params
        ])
        conv_output_dim = sum(out_dim for (out_dim, _) in self.convolutions_params)

        self.highway_layers = nn.ModuleList(
            [HighwayLayer(conv_output_dim)] * self.num_highway_layers
        )

    def forward(self, char_inds_flat):
        x = self.embed_chars(char_inds_flat)

        encoder_padding_mask = char_inds_flat.eq(self.padding_idx)
        if not encoder_padding_mask.any():
            encoder_padding_mask = None

        kernel_outputs = []
        for conv in self.convolutions:
            if encoder_padding_mask is not None:
                x = x.masked_fill(encoder_padding_mask.unsqueeze(-1), 0)
            # conv input: [total_words, char_emb_dim, seq_len]
            # conv output: [total_words, in_channel_dim, seq_len]
            conv_output = conv(x.permute(1, 2, 0))
            kernel_outputs.append(conv_output)
        # Pooling over the entire seq
        pools = [torch.max(conv, dim=2)[0] for conv in kernel_outputs]
        # [total_words, sum(output_channel_dim)]
        encoder_output = torch.cat([p for p in pools], 1)

        for highway_layer in self.highway_layers:
            encoder_output = highway_layer(encoder_output)

        # (total_words, output_dim)
        return encoder_output
