# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2021 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math

import paddle
import paddle.nn as nn

from ...transformers.roberta.modeling import RobertaEmbeddings
from .. import PretrainedModel, register_base_model
from ..activations import get_activation
from .configuration import (
    LUKE_PRETRAINED_INIT_CONFIGURATION,
    LUKE_PRETRAINED_RESOURCE_FILES_MAP,
    LukeConfig,
)

__all__ = [
    "LukeModel",
    "LukePretrainedModel",
    "LukeForEntitySpanClassification",
    "LukeForEntityPairClassification",
    "LukeForEntityClassification",
    "LukeForMaskedLM",
    "LukeForQuestionAnswering",
]


def paddle_gather(x, dim, index):
    index_shape = index.shape
    index_flatten = index.flatten()
    if dim < 0:
        dim = len(x.shape) + dim
    nd_index = []
    for k in range(len(x.shape)):
        if k == dim:
            nd_index.append(index_flatten)
        else:
            reshape_shape = [1] * len(x.shape)
            reshape_shape[k] = x.shape[k]
            x_arange = paddle.arange(x.shape[k], dtype=index.dtype)
            x_arange = x_arange.reshape(reshape_shape)
            dim_index = paddle.expand(x_arange, index_shape).flatten()
            nd_index.append(dim_index)
    ind2 = paddle.transpose(paddle.stack(nd_index), [1, 0]).astype("int64")
    paddle_out = paddle.gather_nd(x, ind2).reshape(index_shape)
    return paddle_out


layer_norm_eps = 1e-6


class LukePretrainedModel(PretrainedModel):
    r"""
    An abstract class for pretrained Luke models. It provides Luke related
    `model_config_file`, `pretrained_init_configuration`, `resource_files_names`,
    `pretrained_resource_files_map`, `base_model_prefix` for downloading and
    loading pretrained models.
    See :class:`~paddlenlp.transformers.model_utils.PretrainedModel` for more details.

    """

    pretrained_init_configuration = LUKE_PRETRAINED_INIT_CONFIGURATION
    pretrained_resource_files_map = LUKE_PRETRAINED_RESOURCE_FILES_MAP

    base_model_prefix = "luke"
    config_class = LukeConfig

    def init_weights(self, layer):
        """Initialization hook"""
        if isinstance(layer, (nn.Linear, nn.Embedding)):
            # only support dygraph, use truncated_normal and make it inplace
            # and configurable later
            layer.weight.set_value(
                paddle.tensor.normal(
                    mean=0.0,
                    std=self.config.initializer_range,
                    shape=layer.weight.shape,
                )
            )
        elif isinstance(layer, nn.LayerNorm):
            layer._epsilon = layer_norm_eps


class LukeSelfOutput(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(LukeSelfOutput, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.layer_norm = nn.LayerNorm(config.hidden_size, epsilon=layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.layer_norm(hidden_states + input_tensor)
        return hidden_states


class LukeIntermediate(nn.Layer):
    def __init__(self, config: LukeConfig):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
        self.intermediate_act_fn = get_activation(config.hidden_act)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class LukeOutput(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(LukeOutput, self).__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.layer_norm = nn.LayerNorm(config.hidden_size, epsilon=layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.layer_norm(hidden_states + input_tensor)
        return hidden_states


class LukeEmbeddings(RobertaEmbeddings):
    """
    Same as BertEmbeddings with a tiny tweak for positional embeddings indexing.
    """

    def __init__(self, config: LukeConfig):
        super(LukeEmbeddings, self).__init__(config)

    def forward(
        self,
        input_ids=None,
        token_type_ids=None,
        position_ids=None,
    ):
        return super(LukeEmbeddings, self).forward(
            input_ids=input_ids, token_type_ids=token_type_ids, position_ids=position_ids
        )


class LukePooler(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(LukePooler, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class EntityEmbeddings(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(EntityEmbeddings, self).__init__()
        self.entity_emb_size = config.entity_emb_size
        self.hidden_size = config.hidden_size
        self.entity_embeddings = nn.Embedding(config.entity_vocab_size, config.entity_emb_size, padding_idx=0)
        if config.entity_emb_size != config.hidden_size:
            self.entity_embedding_dense = nn.Linear(config.entity_emb_size, config.hidden_size, bias_attr=False)

        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)

        self.layer_norm = nn.LayerNorm(config.hidden_size, epsilon=layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, entity_ids, position_ids, token_type_ids=None):
        if token_type_ids is None:
            token_type_ids = paddle.zeros_like(entity_ids)

        entity_embeddings = self.entity_embeddings(entity_ids)
        if self.entity_emb_size != self.hidden_size:
            entity_embeddings = self.entity_embedding_dense(entity_embeddings)

        position_embeddings = self.position_embeddings(position_ids.clip(min=0))
        position_embedding_mask = (position_ids != -1).astype(position_embeddings.dtype).unsqueeze(-1)
        position_embeddings = position_embeddings * position_embedding_mask
        position_embeddings = paddle.sum(position_embeddings, axis=-2)
        position_embeddings = position_embeddings / position_embedding_mask.sum(axis=-2).clip(min=1e-7)

        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = entity_embeddings + position_embeddings + token_type_embeddings
        embeddings = self.layer_norm(embeddings)
        embeddings = self.dropout(embeddings)

        return embeddings


class LukeSelfAttention(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(LukeSelfAttention, self).__init__()
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.w2e_query = nn.Linear(config.hidden_size, self.all_head_size)
        self.e2w_query = nn.Linear(config.hidden_size, self.all_head_size)
        self.e2e_query = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.shape[:-1] + [self.num_attention_heads, self.attention_head_size]
        x = x.reshape(new_x_shape)
        return x.transpose((0, 2, 1, 3))

    def forward(
        self,
        word_hidden_states,
        entity_hidden_states,
        attention_mask=None,
    ):
        word_size = word_hidden_states.shape[1]

        if entity_hidden_states is None:
            concat_hidden_states = word_hidden_states
        else:
            concat_hidden_states = paddle.concat([word_hidden_states, entity_hidden_states], axis=1)

        key_layer = self.transpose_for_scores(self.key(concat_hidden_states))
        value_layer = self.transpose_for_scores(self.value(concat_hidden_states))

        if entity_hidden_states is not None:
            # compute query vectors using word-word (w2w), word-entity (w2e), entity-word (e2w), entity-entity (e2e)
            # query layers
            w2w_query_layer = self.transpose_for_scores(self.query(word_hidden_states))
            w2e_query_layer = self.transpose_for_scores(self.w2e_query(word_hidden_states))
            e2w_query_layer = self.transpose_for_scores(self.e2w_query(entity_hidden_states))
            e2e_query_layer = self.transpose_for_scores(self.e2e_query(entity_hidden_states))

            # compute w2w, w2e, e2w, and e2e key vectors used with the query vectors computed above
            w2w_key_layer = key_layer[:, :, :word_size, :]
            e2w_key_layer = key_layer[:, :, :word_size, :]
            w2e_key_layer = key_layer[:, :, word_size:, :]
            e2e_key_layer = key_layer[:, :, word_size:, :]

            # compute attention scores based on the dot product between the query and key vectors
            w2w_attention_scores = paddle.matmul(w2w_query_layer, w2w_key_layer.transpose((0, 1, 3, 2)))
            w2e_attention_scores = paddle.matmul(w2e_query_layer, w2e_key_layer.transpose((0, 1, 3, 2)))
            e2w_attention_scores = paddle.matmul(e2w_query_layer, e2w_key_layer.transpose((0, 1, 3, 2)))
            e2e_attention_scores = paddle.matmul(e2e_query_layer, e2e_key_layer.transpose((0, 1, 3, 2)))

            # combine attention scores to create the final attention score matrix
            word_attention_scores = paddle.concat([w2w_attention_scores, w2e_attention_scores], axis=3)
            entity_attention_scores = paddle.concat([e2w_attention_scores, e2e_attention_scores], axis=3)
            attention_scores = paddle.concat([word_attention_scores, entity_attention_scores], axis=2)

        else:
            query_layer = self.transpose_for_scores(self.query(concat_hidden_states))
            attention_scores = paddle.matmul(query_layer, key_layer.transpose((0, 1, 3, 2)))

        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in LukeModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.functional.softmax(attention_scores, axis=-1)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        context_layer = paddle.matmul(attention_probs, value_layer)

        context_layer = context_layer.transpose((0, 2, 1, 3))
        new_context_layer_shape = context_layer.shape[:-2] + [
            self.all_head_size,
        ]
        context_layer = context_layer.reshape(new_context_layer_shape)

        output_word_hidden_states = context_layer[:, :word_size, :]
        if entity_hidden_states is None:
            output_entity_hidden_states = None
        else:
            output_entity_hidden_states = context_layer[:, word_size:, :]

        outputs = (output_word_hidden_states, output_entity_hidden_states)

        return outputs


class LukeAttention(nn.Layer):
    def __init__(self, config: LukeConfig):
        super().__init__()
        self.self = LukeSelfAttention(config)
        self.output = LukeSelfOutput(config)

    def forward(
        self,
        word_hidden_states,
        entity_hidden_states,
        attention_mask=None,
    ):
        word_size = word_hidden_states.shape[1]
        self_outputs = self.self(word_hidden_states, entity_hidden_states, attention_mask)
        if entity_hidden_states is None:
            concat_self_outputs = self_outputs[0]
            concat_hidden_states = word_hidden_states
        else:
            concat_self_outputs = paddle.concat(self_outputs[:2], axis=1)
            concat_hidden_states = paddle.concat([word_hidden_states, entity_hidden_states], axis=1)

        attention_output = self.output(concat_self_outputs, concat_hidden_states)

        word_attention_output = attention_output[:, :word_size, :]
        if entity_hidden_states is None:
            entity_attention_output = None
        else:
            entity_attention_output = attention_output[:, word_size:, :]

        # add attentions if we output them
        outputs = (word_attention_output, entity_attention_output) + self_outputs[2:]

        return outputs


class LukeLayer(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(LukeLayer, self).__init__()
        self.seq_len_dim = 1
        self.attention = LukeAttention(config)
        self.intermediate = LukeIntermediate(config)
        self.output = LukeOutput(config)

    def forward(
        self,
        word_hidden_states,
        entity_hidden_states,
        attention_mask=None,
    ):
        word_size = word_hidden_states.shape[1]

        self_attention_outputs = self.attention(
            word_hidden_states,
            entity_hidden_states,
            attention_mask,
        )
        if entity_hidden_states is None:
            concat_attention_output = self_attention_outputs[0]
        else:
            concat_attention_output = paddle.concat(self_attention_outputs[:2], axis=1)

        outputs = self_attention_outputs[2:]  # add self attentions if we output attention weights

        layer_output = self.feed_forward_chunk(concat_attention_output)

        word_layer_output = layer_output[:, :word_size, :]
        if entity_hidden_states is None:
            entity_layer_output = None
        else:
            entity_layer_output = layer_output[:, word_size:, :]

        outputs = (word_layer_output, entity_layer_output) + outputs

        return outputs

    def feed_forward_chunk(self, attention_output):
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output


class LukeEncoder(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(LukeEncoder, self).__init__()
        self.layer = nn.LayerList([LukeLayer(config) for _ in range(config.num_hidden_layers)])

    def forward(
        self,
        word_hidden_states,
        entity_hidden_states,
        attention_mask=None,
    ):

        for i, layer_module in enumerate(self.layer):

            layer_outputs = layer_module(
                word_hidden_states,
                entity_hidden_states,
                attention_mask,
            )

            word_hidden_states = layer_outputs[0]

            if entity_hidden_states is not None:
                entity_hidden_states = layer_outputs[1]

        return word_hidden_states, entity_hidden_states


@register_base_model
class LukeModel(LukePretrainedModel):
    """
    The bare Luke Model transformer outputting raw hidden-states.

    This model inherits from :class:`~paddlenlp.transformers.model_utils.PretrainedModel`.
    Refer to the superclass documentation for the generic methods.

    This model is also a Paddle `paddle.nn.Layer <https://www.paddlepaddle.org.cn/documentation
    /docs/en/api/paddle/fluid/dygraph/layers/Layer_en.html>`__ subclass. Use it as a regular Paddle Layer
    and refer to the Paddle documentation for all matter related to general usage and behavior.

    Args:
        config (:class:`LukeConfig`):
            An instance of LukeConfig.
    """

    def __init__(self, config: LukeConfig):
        super(LukeModel, self).__init__(config)
        self.initializer_range = config.initializer_range
        self.pad_token_id = config.pad_token_id
        self.entity_pad_token_id = config.entity_pad_token_id
        self.encoder = LukeEncoder(config)
        self.embeddings = LukeEmbeddings(config)
        self.entity_embeddings = EntityEmbeddings(config)
        self.pooler = LukePooler(config)
        self.apply(self.init_weights)

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def forward(
        self,
        input_ids,
        token_type_ids=None,
        position_ids=None,
        attention_mask=None,
        entity_ids=None,
        entity_position_ids=None,
        entity_token_type_ids=None,
        entity_attention_mask=None,
    ):
        r"""
        The LukeModel forward method, overrides the `__call__()` special method.

        Args:
            input_ids (Tensor):
                Indices of input sequence tokens in the vocabulary. They are
                numerical representations of tokens that build the input sequence.
                Its data type should be `int64` and it has a shape of [batch_size, sequence_length].
            token_type_ids (Tensor, optional):
                Segment token indices to indicate different portions of the inputs.
                Selected in the range ``[0, type_vocab_size - 1]``.
                If `type_vocab_size` is 2, which means the inputs have two portions.
                Indices can either be 0 or 1:

                - 0 corresponds to a *sentence A* token,
                - 1 corresponds to a *sentence B* token.

                Its data type should be `int64` and it has a shape of [batch_size, sequence_length].
                Defaults to `None`, which means we don't add segment embeddings.
            position_ids(Tensor, optional):
                Indices of positions of each input sequence tokens in the position embeddings. Selected in the range ``[0,
                max_position_embeddings - 1]``.
                Shape as `(batch_size, num_tokens)` and dtype as int64. Defaults to `None`.
            attention_mask (Tensor, optional):
                Mask used in multi-head attention to avoid performing attention on to some unwanted positions,
                usually the paddings or the subsequent positions.
                Its data type can be int, float and bool.
                When the data type is bool, the `masked` tokens have `False` values and the others have `True` values.
                When the data type is int, the `masked` tokens have `0` values and the others have `1` values.
                When the data type is float, the `masked` tokens have `-INF` values and the others have `0` values.
                It is a tensor with shape broadcasted to `[batch_size, num_attention_heads, sequence_length, sequence_length]`.
                Defaults to `None`, which means nothing needed to be prevented attention to.
            entity_ids (Tensor, optional):
                Indices of entity sequence tokens in the entity vocabulary. They are numerical
                representations of entities that build the entity input sequence.
                Its data type should be `int64` and it has a shape of [batch_size, entity_sequence_length].
            entity_position_ids (Tensor, optional):
                Indices of positions of each entity sequence tokens in the position embeddings. Selected in the range ``[0,
                max_position_embeddings - 1]``.
                Shape as `(batch_size, num_entity_tokens)` and dtype as int64. Defaults to `None`.
            entity_token_type_ids (Tensor, optional):
                Segment entity token indices to indicate different portions of the entity inputs.
                Selected in the range ``[0, type_vocab_size - 1]``.
                If `type_vocab_size` is 2, which means the inputs have two portions.
                Indices can either be 0 or 1:
            entity_attention_mask (Tensor, optional):
                Mask used in multi-head attention to avoid performing attention on to some unwanted positions,
                usually the paddings or the subsequent positions.
                Its data type can be int, float and bool.
                When the data type is bool, the `masked` tokens have `False` values and the others have `True` values.
                When the data type is int, the `masked` tokens have `0` values and the others have `1` values.
                When the data type is float, the `masked` tokens have `-INF` values and the others have `0` values.
                It is a tensor will be concat with `attention_mask`.

        Returns:
            tuple: Returns tuple (`word_hidden_state, entity_hidden_state, pool_output`).

            With the fields:

            - `word_hidden_state` (Tensor):
                Sequence of hidden-states at the last layer of the model.
                It's data type should be float32 and its shape is [batch_size, sequence_length, hidden_size].

            - `entity_hidden_state` (Tensor):
                Sequence of entity hidden-states at the last layer of the model.
                It's data type should be float32 and its shape is [batch_size, sequence_length, hidden_size].

            - `pooled_output` (Tensor):
                The output of first token (`<s>`) in sequence.
                We "pool" the model by simply taking the hidden state corresponding to the first token.
                Its data type should be float32 and its shape is [batch_size, hidden_size].

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import LukeModel, LukeTokenizer

                tokenizer = LukeTokenizer.from_pretrained('luke-base')
                model = LukeModel.from_pretrained('luke-base')

                text = "Beyoncé lives in Los Angeles."
                entity_spans = [(0, 7)]
                inputs = tokenizer(text, entity_spans=entity_spans, add_prefix_space=True)
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                output = model(**inputs)
        """

        input_shape = input_ids.shape

        batch_size, seq_length = input_shape

        if attention_mask is None:
            attention_mask = paddle.unsqueeze(
                (input_ids == self.pad_token_id).astype(self.pooler.dense.weight.dtype) * -1e4, axis=[1, 2]
            )
        else:
            if attention_mask.ndim == 2:
                # attention_mask [batch_size, sequence_length] -> [batch_size, 1, 1, sequence_length]
                attention_mask = attention_mask.unsqueeze(axis=[1, 2])
                attention_mask = (1.0 - attention_mask) * -1e4
        if entity_ids is not None:
            entity_seq_length = entity_ids.shape[1]
            if entity_attention_mask is None:
                entity_attention_mask = paddle.unsqueeze(
                    (entity_ids == self.entity_pad_token_id).astype(self.pooler.dense.weight.dtype) * -1e4, axis=[1, 2]
                )
            else:
                if entity_attention_mask.ndim == 2:
                    # attention_mask [batch_size, sequence_length] -> [batch_size, 1, 1, sequence_length]
                    entity_attention_mask = entity_attention_mask.unsqueeze(axis=[1, 2])
                    entity_attention_mask = (1.0 - entity_attention_mask) * -1e4
            if entity_token_type_ids is None:
                entity_token_type_ids = paddle.zeros((batch_size, entity_seq_length), dtype="int64")
            attention_mask = paddle.concat([attention_mask, entity_attention_mask], axis=-1)

        word_embedding_output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
        )

        if entity_ids is None:
            entity_embedding_output = None
        else:
            entity_embedding_output = self.entity_embeddings(entity_ids, entity_position_ids, entity_token_type_ids)

        # Fourth, send embeddings through the model
        encoder_outputs = self.encoder(
            word_embedding_output,
            entity_embedding_output,
            attention_mask=attention_mask,
        )

        sequence_output = encoder_outputs[0]

        pooled_output = self.pooler(sequence_output)

        return sequence_output, encoder_outputs[1], pooled_output


class LukeLMHead(nn.Layer):
    """Luke Head for masked language modeling."""

    def __init__(self, config: LukeConfig, embedding_weights=None):
        super(LukeLMHead, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.layer_norm = nn.LayerNorm(config.hidden_size, epsilon=layer_norm_eps)
        self.activation = get_activation(config.hidden_act)
        self.decoder_weight = (
            self.create_parameter(
                shape=[config.vocab_size, config.hidden_size], dtype=self.transform.weight.dtype, is_bias=False
            )
            if embedding_weights is None
            else embedding_weights
        )
        self.decoder_bias = self.create_parameter(
            shape=[config.vocab_size], dtype=self.decoder_weight.dtype, is_bias=True
        )

    def forward(self, features, **kwargs):
        hidden_state = self.dense(features)
        hidden_state = self.activation(hidden_state)
        hidden_state = self.layer_norm(hidden_state)
        hidden_state = paddle.tensor.matmul(hidden_state, self.decoder_weight, transpose_y=True) + self.decoder_bias
        return hidden_state


class EntityPredictionHeadTransform(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(EntityPredictionHeadTransform, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.entity_emb_size)
        self.transform_act_fn = get_activation(config.hidden_act)
        self.layer_norm = nn.LayerNorm(config.entity_emb_size, epsilon=layer_norm_eps)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.layer_norm(hidden_states)
        return hidden_states


class EntityPredictionHead(nn.Layer):
    def __init__(self, config: LukeConfig):
        super(EntityPredictionHead, self).__init__()
        self.transform = EntityPredictionHeadTransform(config)
        self.decoder = nn.Linear(config.entity_emb_size, config.entity_vocab_size)

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states)
        return hidden_states


class LukeForMaskedLM(LukePretrainedModel):
    """
    Luke Model with a `masked language modeling` head on top.

    Args:
        config (:class:`LukeConfig`):
            An instance of LukeConfig.

    """

    def __init__(self, config: LukeConfig):
        super(LukeForMaskedLM, self).__init__(config)
        self.luke = LukeModel(config)
        self.vocab_size = self.config.vocab_size
        self.entity_vocab_size = self.config.entity_vocab_size

        self.lm_head = LukeLMHead(
            config,
            embedding_weights=self.luke.embeddings.word_embeddings.weight,
        )
        self.entity_predictions = EntityPredictionHead(config)

        self.apply(self.init_weights)

    def forward(
        self,
        input_ids,
        token_type_ids=None,
        position_ids=None,
        attention_mask=None,
        entity_ids=None,
        entity_position_ids=None,
        entity_token_type_ids=None,
        entity_attention_mask=None,
    ):
        r"""
        The LukeForMaskedLM forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor):
                See :class:`LukeModel`.
            token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            position_ids (Tensor, optional):
                See :class: `LukeModel`
            attention_mask (list, optional):
                See :class:`LukeModel`.
            entity_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_position_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_attention_mask (list, optional):
                See :class:`LukeModel`.

        Returns:
            tuple: Returns tuple (``logits``, ``entity_logits``).

            With the fields:

            - `logits` (Tensor):
                The scores of masked token prediction.
                Its data type should be float32 and shape is [batch_size, sequence_length, vocab_size].

            - `entity_logits` (Tensor):
                The scores of masked entity prediction.
                Its data type should be float32 and its shape is [batch_size, entity_length, entity_vocab_size].

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import LukeForMaskedLM, LukeTokenizer

                tokenizer = LukeTokenizer.from_pretrained('luke-base')
                model = LukeForMaskedLM.from_pretrained('luke-base')

                text = "Beyoncé lives in Los Angeles."
                entity_spans = [(0, 7)]
                inputs = tokenizer(text, entity_spans=entity_spans, add_prefix_space=True)
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                logits, entity_logits = model(**inputs)
        """

        outputs = self.luke(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            entity_ids=entity_ids,
            entity_position_ids=entity_position_ids,
            entity_token_type_ids=entity_token_type_ids,
            entity_attention_mask=entity_attention_mask,
        )

        logits = self.lm_head(outputs[0])
        entity_logits = self.entity_predictions(outputs[1])

        return logits, entity_logits


class LukeForEntityClassification(LukePretrainedModel):
    """
    The LUKE model with a classification head on top (a linear layer on top of the hidden state of the first entity
    token) for entity classification tasks, such as Open Entity.

    Args:
        config (:class:`LukeConfig`):
            An instance of LukeConfig.
    """

    def __init__(self, config: LukeConfig):
        super(LukeForEntityClassification, self).__init__(config)

        self.luke = LukeModel(config)

        self.num_labels = config.num_labels
        self.dropout = nn.Dropout(self.config.hidden_dropout_prob)
        self.classifier = nn.Linear(self.config.hidden_size, config.num_labels)
        self.apply(self.init_weights)

    def forward(
        self,
        input_ids,
        token_type_ids=None,
        position_ids=None,
        attention_mask=None,
        entity_ids=None,
        entity_position_ids=None,
        entity_token_type_ids=None,
        entity_attention_mask=None,
    ):
        r"""
        The LukeForEntityClassification forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor):
                See :class:`LukeModel`.
            token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            position_ids (Tensor, optional):
                See :class: `LukeModel`
            attention_mask (list, optional):
                See :class:`LukeModel`.
            entity_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_position_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_attention_mask (list, optional):
                See :class:`LukeModel`.

        Returns:
            Tensor: Returns tensor `logits`, a tensor of the entity classification logits.
            Shape as `[batch_size, num_labels]` and dtype as float32.

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import LukeForEntityClassification, LukeTokenizer

                tokenizer = LukeTokenizer.from_pretrained('luke-base')
                model = LukeForEntityClassification.from_pretrained('luke-base', num_labels=2)

                text = "Beyoncé lives in Los Angeles."
                entity_spans = [(0, 7)]
                inputs = tokenizer(text, entity_spans=entity_spans, add_prefix_space=True)
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                logits = model(**inputs)
        """

        outputs = self.luke(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            entity_ids=entity_ids,
            entity_position_ids=entity_position_ids,
            entity_token_type_ids=entity_token_type_ids,
            entity_attention_mask=entity_attention_mask,
        )

        feature_vector = outputs[1][:, 0, :]
        feature_vector = self.dropout(feature_vector)
        logits = self.classifier(feature_vector)

        return logits


class LukeForEntityPairClassification(LukePretrainedModel):
    """
    The LUKE model with a classification head on top (a linear layer on top of the hidden states of the two entity
    tokens) for entity pair classification tasks, such as TACRED.

    Args:
        config (:class:`LukeConfig`):
            An instance of LukeConfig.

    """

    def __init__(self, config: LukeConfig):
        super(LukeForEntityPairClassification, self).__init__(config)

        self.luke = LukeModel(config)

        self.num_labels = config.num_labels
        self.dropout = nn.Dropout(self.config.hidden_dropout_prob)
        self.classifier = nn.Linear(self.config.hidden_size * 2, config.num_labels, bias_attr=False)
        self.apply(self.init_weights)

    def forward(
        self,
        input_ids,
        token_type_ids=None,
        position_ids=None,
        attention_mask=None,
        entity_ids=None,
        entity_position_ids=None,
        entity_token_type_ids=None,
        entity_attention_mask=None,
    ):
        r"""
        The LukeForEntityPairClassification forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor):
                See :class:`LukeModel`.
            token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            position_ids (Tensor, optional):
                See :class: `LukeModel`
            attention_mask (list, optional):
                See :class:`LukeModel`.
            entity_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_position_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_attention_mask (list, optional):
                See :class:`LukeModel`.

        Returns:
            Tensor: Returns tensor `logits`, a tensor of the entity pair classification logits.
            Shape as `[batch_size, num_labels]` and dtype as float32.

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import LukeForEntityPairClassification, LukeTokenizer

                tokenizer = LukeTokenizer.from_pretrained('luke-base')
                model = LukeForEntityPairClassification.from_pretrained('luke-base', num_labels=2)

                text = "Beyoncé lives in Los Angeles."
                entity_spans = [(0, 7), (17, 28)]
                inputs = tokenizer(text, entity_spans=entity_spans, add_prefix_space=True)
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                logits = model(**inputs)
        """

        outputs = self.luke(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            entity_ids=entity_ids,
            entity_position_ids=entity_position_ids,
            entity_token_type_ids=entity_token_type_ids,
            entity_attention_mask=entity_attention_mask,
        )

        feature_vector = paddle.concat([outputs[1][:, 0, :], outputs[1][:, 1, :]], axis=1)
        feature_vector = self.dropout(feature_vector)
        logits = self.classifier(feature_vector)

        return logits


class LukeForEntitySpanClassification(LukePretrainedModel):
    """
    The LUKE model with a span classification head on top (a linear layer on top of the hidden states output) for tasks
    such as named entity recognition.

    Args:
        config (:class:`LukeConfig`):
            An instance of LukeConfig.
    """

    def __init__(self, config: LukeConfig):
        super(LukeForEntitySpanClassification, self).__init__(config)

        self.luke = LukeModel(config)

        self.num_labels = config.num_labels
        self.dropout = nn.Dropout(self.config.hidden_dropout_prob)
        self.classifier = nn.Linear(self.config.hidden_size * 3, config.num_labels)
        self.apply(self.init_weights)

    def forward(
        self,
        entity_start_positions,
        entity_end_positions,
        input_ids,
        token_type_ids=None,
        position_ids=None,
        attention_mask=None,
        entity_ids=None,
        entity_position_ids=None,
        entity_token_type_ids=None,
        entity_attention_mask=None,
    ):
        r"""
        The LukeForEntitySpanClassification forward method, overrides the __call__() special method.

        Args:
            entity_start_positions:
                The start position of entities in sequence.
            entity_end_positions:
                The start position of entities in sequence.
            input_ids (Tensor):
                See :class:`LukeModel`.
            token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            position_ids (Tensor, optional):
                See :class: `LukeModel`
            attention_mask (list, optional):
                See :class:`LukeModel`.
            entity_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_position_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_attention_mask (list, optional):
                See :class:`LukeModel`.

        Returns:
            Tensor: Returns tensor `logits`, a tensor of the entity span classification logits.
            Shape as `[batch_size, num_entities, num_labels]` and dtype as float32.

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import LukeForEntitySpanClassification, LukeTokenizer

                tokenizer = LukeTokenizer.from_pretrained('luke-base')
                model = LukeForEntitySpanClassification.from_pretrained('luke-base', num_labels=2)

                text = "Beyoncé lives in Los Angeles."
                entity_spans = [(0, 7)]
                inputs = tokenizer(text, entity_spans=entity_spans, add_prefix_space=True)
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                inputs['entity_start_positions'] = paddle.to_tensor([[1]], dtype='int64')
                inputs['entity_end_positions'] = paddle.to_tensor([[2]], dtype='int64')
                logits = model(**inputs)
        """

        outputs = self.luke(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            entity_ids=entity_ids,
            entity_position_ids=entity_position_ids,
            entity_token_type_ids=entity_token_type_ids,
            entity_attention_mask=entity_attention_mask,
        )
        hidden_size = outputs[0].shape[-1]

        entity_start_positions = entity_start_positions.unsqueeze(-1).expand((-1, -1, hidden_size))
        start_states = paddle_gather(x=outputs[0], index=entity_start_positions, dim=-2)
        entity_end_positions = entity_end_positions.unsqueeze(-1).expand((-1, -1, hidden_size))
        end_states = paddle_gather(x=outputs[0], index=entity_end_positions, dim=-2)
        feature_vector = paddle.concat([start_states, end_states, outputs[1]], axis=2)

        feature_vector = self.dropout(feature_vector)
        logits = self.classifier(feature_vector)

        return logits


class LukeForQuestionAnswering(LukePretrainedModel):
    """
    LukeBert Model with question answering tasks.
    Args:
        config (:class:`LukeConfig`):
            An instance of LukeConfig.
    """

    def __init__(self, config: LukeConfig):
        super(LukeForQuestionAnswering, self).__init__(config)
        self.luke = LukeModel(config)
        self.qa_outputs = nn.Linear(self.config.hidden_size, 2)
        self.apply(self.init_weights)

    def forward(
        self,
        input_ids=None,
        token_type_ids=None,
        position_ids=None,
        attention_mask=None,
        entity_ids=None,
        entity_position_ids=None,
        entity_token_type_ids=None,
        entity_attention_mask=None,
    ):
        r"""
        The LukeForQuestionAnswering forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor):
                See :class:`LukeModel`.
            token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            position_ids (Tensor, optional):
                See :class: `LukeModel`
            attention_mask (list, optional):
                See :class:`LukeModel`.
            entity_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_position_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_token_type_ids (Tensor, optional):
                See :class:`LukeModel`.
            entity_attention_mask (list, optional):
                See :class:`LukeModel`.

        Returns:
            tuple: Returns tuple (`start_logits`, `end_logits`).
            With the fields:
            - `start_logits` (Tensor):
                A tensor of the input token classification logits, indicates the start position of the labelled span.
                Its data type should be float32 and its shape is [batch_size, sequence_length].
            - `end_logits` (Tensor):
                A tensor of the input token classification logits, indicates the end position of the labelled span.
                Its data type should be float32 and its shape is [batch_size, sequence_length].

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import LukeForQuestionAnswering, LukeTokenizer

                tokenizer = LukeTokenizer.from_pretrained('luke-base')
                model = LukeForQuestionAnswering.from_pretrained('luke-base')

                text = "Beyoncé lives in Los Angeles."
                entity_spans = [(0, 7)]
                inputs = tokenizer(text, entity_spans=entity_spans, add_prefix_space=True)
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                start_logits, end_logits = model(**inputs)
        """

        encoder_outputs = self.luke(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            entity_ids=entity_ids,
            entity_position_ids=entity_position_ids,
            entity_token_type_ids=entity_token_type_ids,
            entity_attention_mask=entity_attention_mask,
        )

        word_hidden_states = encoder_outputs[0][:, : input_ids.shape[1], :]
        logits = self.qa_outputs(word_hidden_states)
        start_logits, end_logits = paddle.split(logits, 2, -1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        return start_logits, end_logits
