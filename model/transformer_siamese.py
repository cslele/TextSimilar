#!/usr/bin python3
# -*- coding: utf-8 -*-
# @Time    : 19-1-22 上午10:48
# @Author  : 林利芳
# @File    : transformer_siamese.py
import tensorflow as tf
from config.hyperparams import HyperParams as hp
from model.module.modules import embedding, positional_encoding, multihead_attention, feedforward, layer_normalize


class TransformerSiameseNetwork(object):
	def __init__(self, vocab_size, embedding_size, max_len, batch_size, is_training=True, seg='LSTM'):
		self.vocab_size = vocab_size
		self.embedding_size = embedding_size
		self.max_len = max_len
		self.is_training = is_training
		self.graph = tf.Graph()
		with self.graph.as_default():
			self.left_x = tf.placeholder(tf.int32, shape=(batch_size, max_len), name="left_x")
			self.right_x = tf.placeholder(tf.int32, shape=(batch_size, max_len), name="right_x")
			self.y = tf.placeholder(tf.int32, shape=(batch_size,), name="target")
			self.left_seq_lens = tf.placeholder(dtype=tf.int32, shape=[batch_size])
			self.right_seq_lens = tf.placeholder(dtype=tf.int32, shape=[batch_size])
			self.global_step = tf.train.create_global_step()
			
			query, key = self.siamese(seg)
			self.distance, self.pre_y = self.similar(query, key)
			self.accuracy = self.predict()
			self.loss = self.loss_layer()
			self.train_op = self.optimize()
	
	def siamese(self, seg):
		"""
		孪生网络 transformer + rnn
		:param seg:
		:return:
		"""
		x = tf.concat([self.left_x, self.right_x], axis=0)
		seq_lens = tf.concat([self.left_seq_lens, self.right_seq_lens], axis=0)
		# layers embedding multi_head_attention rnn
		left_embed = embedding(self.left_x, vocab_size=self.vocab_size, num_units=hp.num_units, scale=True,
							   scope="lembed")
		right_embed = embedding(self.right_x, vocab_size=self.vocab_size, num_units=hp.num_units, scale=True,
								scope="rembed")
		
		query, key = self.transformer(left_embed, right_embed)
		# output = self.rnn_layer(embed, seq_lens, seg)
		query = self.attention(query, query)
		key = self.attention(key, key)
		return query, key
	
	def rnn_layer(self, inputs, seq_lens, seg):
		"""
		创建双向RNN层
		:param inputs:
		:param seq_lens:
		:param seg: LSTM GRU F-LSTM, IndRNN
		:return:
		"""
		if seg == 'LSTM':
			fw_cell = tf.nn.rnn_cell.BasicLSTMCell(num_units=hp.num_units)
			bw_cell = tf.nn.rnn_cell.BasicLSTMCell(num_units=hp.num_units)
		
		elif seg == 'GRU':
			fw_cell = tf.nn.rnn_cell.GRUCell(num_units=hp.num_units)
			bw_cell = tf.nn.rnn_cell.GRUCell(num_units=hp.num_units)
		else:
			fw_cell = tf.nn.rnn_cell.BasicRNNCell(num_units=hp.num_units)
			bw_cell = tf.nn.rnn_cell.BasicRNNCell(num_units=hp.num_units)
		# 双向rnn
		(fw_output, bw_output), _ = tf.nn.bidirectional_dynamic_rnn(
			fw_cell, bw_cell, inputs, sequence_length=seq_lens, dtype=tf.float32)
		# 合并双向rnn的output batch_size * max_seq * (hidden_dim*2)
		output = tf.add(fw_output, bw_output)
		return output
	
	def transformer(self, query, key):
		with tf.variable_scope("Transformer_Encoder"):
			# Positional Encoding
			query += positional_encoding(self.left_x, num_units=hp.num_units, zero_pad=False, scale=False)
			key += positional_encoding(self.right_x, num_units=hp.num_units, zero_pad=False, scale=False)
			# Dropout
			output = self.multi_head_block(query, key)
			return output
	
	def multi_head_block(self, query, key, causality=False):
		"""
		多头注意力机制
		:param query:
		:param key:
		:param causality:
		:return:
		"""
		for i in range(hp.num_blocks):
			with tf.variable_scope("num_blocks_{}".format(i)):
				# multi head Attention ( self-attention)
				query = self.multihead_attention(query, query, name="query_attention", causality=causality)
				key = self.multihead_attention(key, key, name="key_attention", causality=causality)
				query = self.multihead_attention(query, key, name="query_key_attention")
				key = self.multihead_attention(key, query, name="query_key_attention")
		return query, key
	
	def multihead_attention(self, query, key, name="key_attention", causality=False):
		value = multihead_attention(
			queries=query, keys=key, num_units=hp.num_units, num_heads=hp.num_heads,
			dropout_rate=hp.dropout_rate, is_training=self.is_training, causality=causality,
			scope=name)
		# Feed Forward
		value = feedforward(value, num_units=[4 * hp.num_units, hp.num_units])
		return value
	
	def loss_layer(self):
		"""
		损失函数 L+ = （1-Ew)^2/4  L_ = max(Ex,0)^2
		:return:
		"""
		y = tf.cast(self.y, tf.float32)
		with tf.name_scope("output"):
			loss_p = tf.square(1 - self.distance) / 4
			mask = tf.sign(tf.nn.relu(self.distance - hp.margin))
			loss_m = tf.square(mask * self.distance)
			loss = tf.reduce_sum(y * loss_p + (1 - y) * loss_m)
			return loss
	
	def attention(self, embed, query):
		"""
		注意力机制
		:param embed:
		:param query:
		:return:
		"""
		with tf.name_scope("attention"):
			w = tf.get_variable(name="attention_w", shape=[2 * hp.num_units, hp.attention_size], dtype=tf.float32)
			b = tf.get_variable(name="attention_b", shape=[hp.attention_size], dtype=tf.float32)
			u = tf.get_variable(name="attention_u", shape=[hp.attention_size, 1], dtype=tf.float32)
			value = tf.concat([embed, query], axis=-1)
			value = tf.reshape(value, [-1, 2 * hp.num_units])
			attention = tf.matmul(tf.tanh(tf.matmul(value, w) + b), u)
			attention = tf.reshape(attention, shape=[-1, self.max_len])
			attention = tf.nn.softmax(attention, axis=-1)
			attention = tf.tile(tf.expand_dims(attention, axis=-1), multiples=[1, 1, hp.num_units])
			
			output = tf.reduce_sum(attention * query, axis=1)
			output = layer_normalize(output)
			return output
	
	@staticmethod
	def similar(query, key):
		"""
		cosine(key,value) = key * value/(|key|*|value|)
		:param key:
		:param value:
		:return:
		"""
		dot_value = tf.reduce_sum(query * key, axis=-1)
		query_sqrt = tf.sqrt(tf.reduce_sum(tf.square(query), axis=-1) + hp.eps)
		key_sqrt = tf.sqrt(tf.reduce_sum(tf.square(key), axis=-1) + hp.eps)
		distance = tf.div(dot_value, key_sqrt * query_sqrt, name="similar")
		pre_y = tf.sign(tf.nn.relu(distance - hp.margin))
		pre_y = tf.cast(pre_y, tf.int32, name='pre')
		return distance, pre_y
	
	def predict(self):
		correct_predictions = tf.equal(self.pre_y, self.y)
		accuracy = tf.reduce_mean(tf.cast(correct_predictions, "float"), name="accuracy")
		return accuracy
	
	def optimize(self):
		"""
		优化器
		:return:
		"""
		optimizer = tf.train.AdamOptimizer(learning_rate=hp.lr, beta1=0.9, beta2=0.98, epsilon=1e-8)
		train_op = optimizer.minimize(self.loss, global_step=self.global_step)
		return train_op
