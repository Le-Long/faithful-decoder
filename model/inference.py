#!/usr/bin/env python
# coding=utf-8
# Copyright 2021 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
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
"""
Fine-tuning the library models for sequence to sequence.
"""
# You can also adapt this script on your own sequence to sequence task. Pointers for this are left as comments.

import argparse
import os
import time

from evaluate import load
from datasets import load_from_disk, load_metric
import nltk  # Here to have a nice missing dependency error message early on
import numpy as np
import pandas as pd
from transformers import (
    AutoConfig,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    T5Tokenizer,
    LongT5ForConditionalGeneration,
    LEDForConditionalGeneration,
    AdamW
)
from tqdm import tqdm
import torch
from torch.utils.data import DataLoader
from bert_score import BERTScorer

nltk.download('punkt')
# from model.utils import add_global_attention_mask


def add_global_attention_mask(batch):
    global_attention_mask = torch.zeros_like(batch['input_ids']).to(batch['input_ids'].device)
    # put global attention on <s> token
    global_attention_mask[:, 0] = 1
    batch['global_attention_mask'] = global_attention_mask


dirname = os.path.dirname(__file__)
DATA_DIR = os.path.expanduser('../data_tmp')
T5_MODEL = 'google/long-t5-tglobal-base'
PRIMERA_MODEL = 'allenai/PRIMERA'


def compute_rouge(metric, reference, prediction):
    result = metric.compute(references=[reference], predictions=[prediction], use_stemmer=True)
    result = {key: value.mid.fmeasure * 100 for key, value in result.items()}
    return result


def train_optimizer(model, dataloader, tokenizer, gen_kwargs, args):
    batch_size = args.batch_size
    discriminator = BERTScorer(
        batch_size=batch_size,
        device=args.device,
        model_type='allenai/scibert_scivocab_uncased',
        use_fast_tokenizer=True, lang='en'
    )
    model.init_discriminator(discriminator) 
    stored_sentences = []

    for batch in tqdm(dataloader, total=len(dataloader)):
        if args.hf_model == 'primera':
            add_global_attention_mask(batch)
            gen_kwargs['global_attention_mask'] = batch['global_attention_mask'].to(args.device)

        model.set_biases(batch_size, seq_len + batch['input_ids'].shape[1], 'non_toxic', 0.7)
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if "biases" in n or "trainable_weights" in n],
                "weight_decay": args.weight_decay,
            }
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
        model.eval()
        minimun_loss = [100000] * batch_size
        stored_sentence = [""] * batch_size
        start_time = time.time()
        for i in range(args.num_train_epochs):
            if all([loss < 0.0003 for loss in minimun_loss]):
                break
            if i % 1 == 0:
                loss, output_ids, gpt_logit, senti_losses = model.soft_forward(
                    batch['input_ids'].to(args.device),
                    attention_mask=batch['attention_mask'].to(args.device),
                    tokenizer=tokenizer,
                    **gen_kwargs,)
                print("Decoding: ", loss)
                sentences = tokenizer.batch_decode(output_ids, skip_special_tokens=True)
                print(sentences)
            loss.backward()
            if i % 1 == 0:
                optimizer.step()
                noise = [torch.normal(mean=0.01, std=0.01, size=model.biases[0].shape,
                                     device=args.device, requires_grad=False) for _ in range(len(model.biases))]
                for i in range(len(model.biases)):
                    model.biases[i].data = model.biases[i].data + noise[i]
            if i % 1 == 0:
                print(f"loss: {loss}")
                for idx in range(batch_size):
                    print(f"loss {idx}: senti loss: {senti_losses[idx]}")
                    if senti_losses[idx] < minimun_loss[idx]:
                        print(f"update minimun loss{idx}")
                        minimun_loss[idx] = senti_losses[idx]
                        stored_sentence[idx] = sentences[idx]
            
        end_time = time.time()
        print("minimun loss: ", minimun_loss)
        print("time: ", end_time - start_time)
    
    stored_sentences.append(stored_sentence)
    return stored_sentences


def main(args):
    # Either PRIMERA (LED) or T5
    is_t5 = args.hf_model.lower() == 't5'
    args.hf_path = T5_MODEL if is_t5 else PRIMERA_MODEL
    model_constructor = LongT5ForConditionalGeneration if is_t5 else LEDForConditionalGeneration
    tokenizer_constructor = T5Tokenizer if is_t5 else AutoTokenizer
    args.max_source_length = 16384 if is_t5 else 4096

    data_prefix = 't5' if is_t5 else 'primera'
    data_path = os.path.join(DATA_DIR, args.dataset, f'{data_prefix}_splits')

    weight_dir = os.path.join(DATA_DIR, 'weights')
    experiment_dir = os.path.join(weight_dir, args.experiment)
    args.output_dir = os.path.join(experiment_dir, args.results_name)
    os.makedirs(args.output_dir, exist_ok=True)
    print(f'Saving all outputs to {args.output_dir}')

    out_fn = os.path.join(args.output_dir, f'{args.split}_predictions.csv')
    if os.path.exists(out_fn) and not args.overwrite:
        print(f'Run with -overwrite to re-produce {out_fn}')
        exit(0)

    ckpt_dir = os.path.join(experiment_dir, args.ckpt_name)
    tokenizer_dir = os.path.join(experiment_dir, 'tokenizer')

    print(f'Loading config from {args.hf_path}')
    config = AutoConfig.from_pretrained(args.hf_path)
    print(f'Loading tokenizer from {tokenizer_dir}')

    tokenizer = tokenizer_constructor.from_pretrained(tokenizer_dir)

    config.vocab_size = len(tokenizer)
    config.contrastive_classifier = args.contrast_classifier  # Can remove if not using margin
    print(f'Loading model from {ckpt_dir}')

    try:
        model = model_constructor.from_pretrained(ckpt_dir, from_tf=False, config=config).to(args.device)
    except Exception as e:
        print(str(e))
        print('Probably erased. We can load the model weights directly instead')
        fn = os.path.join(ckpt_dir, 'pytorch_model.bin')
        fp_weights = torch.load(fn)
        model = LEDForConditionalGeneration(config=config).half()
        model.load_state_dict(fp_weights['module'], strict=False)
        model = model.to(args.device)

    model.resize_token_embeddings(len(tokenizer))
    print(f'Loading custom dataset from {data_path}')
    predict_dataset = load_from_disk(data_path)[args.split]
    uuids = predict_dataset['uuid']

    dataset_cols = list(predict_dataset.features.keys())
    important_cols = [x for x in dataset_cols if x not in {'input_ids', 'attention_mask', 'labels'}]

    if args.max_examples is not None and args.max_examples < len(predict_dataset):
        predict_dataset = predict_dataset.select(range(args.max_examples))

    # If pragmatic model used
    priors = model.initialize_worldpriors(args.batch_size*args.num_beams, args.max_length, 1)
        

    # Data collator
    if is_t5:
        pad_multiple = None
    else:
        pad_multiple = max(model.config.attention_window)
    
    data_collator = DataCollatorForSeq2Seq(
        tokenizer,
        model=model,
        label_pad_token_id=-100,
        pad_to_multiple_of=pad_multiple,
    )

    dataloader = DataLoader(
        predict_dataset.remove_columns(important_cols), shuffle=False, batch_size=args.batch_size,
        collate_fn=data_collator
    )

    # Metric
    metric = load_metric('rouge')

    def postprocess_text(preds):
        preds = [pred.strip() for pred in preds]
        # rougeLSum expects newline after each sentence
        return ['\n'.join(nltk.sent_tokenize(pred)) for pred in preds]

    print('Starting to evaluate run...')
    model = model.eval()
    if args.hf_model == 'primera':
        model = model.half()

    gen_kwargs = {
        'max_length': args.max_length,
        'num_beams': args.num_beams, 'no_repeat_ngram_size': 3, 'early_stopping': True,
        'length_penalty': args.length_penalty, # "num_return_sequences": 5,
        # 'do_sample': True, 'top_k': 0, 'top_p': 0.95
        # 'mature_layer': 12, 'base_layer': 6, 'dola_decoding': True
        'candidate_premature_layers': [12],
        'priors': priors,
    }

    if args.optimization:
        # Optimize outputs with data-dependent biases
        outputs = train_optimizer(model, dataloader, tokenizer, gen_kwargs, args)
    else:

        outputs = []
        beam_outputs = []
        data_idx = 0
        for batch in tqdm(dataloader, total=len(dataloader)):
            if 'priors' in gen_kwargs.keys():
                sz_1 = batch['input_ids'].shape[0]
                sz_2 = args.max_length if args.max_length < batch['input_ids'].shape[1] else (batch['input_ids'].shape[1]/2)
                batch['input_ids'] = batch['input_ids'].repeat(model.world_cardinality, 1)
                batch['attention_mask'] = batch['attention_mask'].repeat(model.world_cardinality, 1)
                batch['attention_mask'][sz_1:, :sz_2] = 0

            if args.hf_model == 'primera':
                add_global_attention_mask(batch)
                gen_kwargs['global_attention_mask'] = batch['global_attention_mask'].to(args.device)
            
            with torch.no_grad(), torch.cuda.amp.autocast() if args.hf_model == 'primera' else torch.no_grad():
                generated_outputs = model.generate(
                    batch['input_ids'].to(args.device),
                    attention_mask=batch['attention_mask'].to(args.device),
                    # output_hidden_states=True,
                    # return_dict_in_generate=True,
                    **gen_kwargs,
                )  
                
                # beam_outputs = list(torch.flatten(token_state[-1]) for token_state in generated_outputs.decoder_hidden_states)
                # print(torch.quantile(torch.cat(beam_outputs), 0.95, interpolation='midpoint'))
                # print(torch.quantile(torch.cat(beam_outputs), 0.99, interpolation='midpoint'))
                generated_tokens = generated_outputs[:sz_1].cpu().numpy()
                # generated_tokens = [generated_outputs[i] for i in range(0, len(generated_outputs), gen_kwargs["num_return_sequences"])]

                labels = batch['labels'].numpy()
                labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
            
                decoded_preds = tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)
                decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)

                prepared_preds = postprocess_text(decoded_preds)
                references = postprocess_text(decoded_labels)

                # Save a file for all candidates each sample
                # beam_idx = 0
                # clean_candidates = postprocess_text(tokenizer.batch_decode(generated_outputs, skip_special_tokens=True))
                # beam_candidates = []

                # for i in range(len(clean_candidates)):
                #     clean_candidate = clean_candidates[i]
                #     beam_row = {'prediction': clean_candidate, 'abstract': decoded_labels[beam_idx], 'uuid': uuids[beam_idx]}
                #     beam_row.update(compute_rouge(metric, reference=references[beam_idx], prediction=clean_candidate))
                #     beam_candidates.append(beam_row)
                #     if i % gen_kwargs["num_return_sequences"] == gen_kwargs["num_return_sequences"] - 1:
                #         beam_outputs.append(max(beam_candidates, key=lambda x:(x['rouge1'] + x['rouge2'] + x['rougeL'])/3))
                #         beam_candidates = []
                #         beam_idx += 1
                
                for clean_prediction, clean_label, prediction, reference in zip(decoded_preds, decoded_labels, prepared_preds, references):
                    output_row = {'prediction': clean_prediction, 'abstract': clean_label, 'uuid': uuids[data_idx]}
                    output_row.update(compute_rouge(metric, reference=reference, prediction=prediction))

                    outputs.append(output_row)
                    data_idx += 1


    outputs = pd.DataFrame(outputs)
    print(f'Saving {len(outputs)} outputs to {out_fn}')
    outputs.to_csv(out_fn, mode='a', index=False)
    # beam_outputs = pd.DataFrame(beam_outputs)
    # candidates_fn = os.path.join(args.output_dir, f'{args.split}_candidates.csv')
    # beam_outputs.to_csv(candidates_fn, mode='a', index=False)

    rouge_cols = ['rouge1', 'rouge2', 'rougeL']
    for col in rouge_cols:
        print(f'{col}: {round(outputs[col].dropna().mean(), 2)}')

    pred_len = outputs['prediction'].apply(lambda x: len(x.split(' ')))
    print(f'Token Lengths: {np.mean(pred_len)}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Inference for summarization models')

    parser.add_argument('--hf_model', default='primera', choices=['primera', 't5'])
    parser.add_argument('--experiment', default='long_t5_ft_pubmed')  # WandB name
    parser.add_argument('--ckpt_name', default='best_ckpt')
    parser.add_argument('--results_name', default='results')
    parser.add_argument('--num_beams', default=1, type=int)
    parser.add_argument('--max_examples', default=10000, type=int)
    parser.add_argument('--batch_size', default=8, type=int)
    parser.add_argument('--device', default=0, type=int)
    parser.add_argument('--dataset')
    parser.add_argument('-overwrite', default=False, action='store_true')
    parser.add_argument('-contrast_classifier', default=False, action='store_true')
    parser.add_argument('--split', default='test')
    parser.add_argument('--max_length', default=1024, type=int)
    parser.add_argument('--length_penalty', default=1.0, type=float)
    parser.add_argument('--weight_decay', default=0.01, type=float)
    parser.add_argument('--learning_rate', default=0.025, type=float)
    parser.add_argument('--num_train_epochs', default=8, type=int)
    parser.add_argument('--optimization', default=False, action='store_true')

    args = parser.parse_args()

    if args.dataset is None:
        if 'pubmed' in args.experiment:
            args.dataset = 'pubmed'
        elif 'chem' in args.experiment:
            args.dataset = 'chemistry'
        elif 'clin' in args.experiment:
            args.dataset = 'clinical'
        else:
            raise Exception(f'Could not infer dataset from {args.fn}. Please set explicitly with --dataset flag.')

    if 'faith' in args.experiment and args.dataset == 'pubmed':
        args.max_length = min(args.max_length, 384)
        print('Ensuring maximum length is 384 for Pubmed')
    elif 'faith' in args.experiment and args.dataset == 'chemistry':
        args.max_length = min(args.max_length, 512)
        print('Ensuring maximum length is 512 for Chemistry')

    main(args)
