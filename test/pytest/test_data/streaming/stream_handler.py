import json
import logging
import types
from copy import copy

import torch
from transformers import AutoTokenizer, GPT2LMHeadModel
from ts.torch_handler.base_handler import BaseHandler

logger = logging.getLogger(__name__)


class StreamingHandler(BaseHandler):
    def initialize(self, ctx):
        super().initialize(ctx)

        ctx.cache = {}

        logger.info(f"Initialized {self.__class__}")
        
        self.tokenizer = AutoTokenizer.from_pretrained("gpt2")
        self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        self.model = GPT2LMHeadModel.from_pretrained("gpt2")
        
        old_update = self.model._update_model_kwargs_for_generation
        ctx.kv_cache = {}
        
        def extract_past_key_values_func(self,*args, **kwargs):
            ctx.kv_cache["past_key_values"] = args[0]["past_key_values"]
            return old_update(*args, **kwargs)

        self.model._update_model_kwargs_for_generation = types.MethodType(extract_past_key_values_func, self.model)
        

    def preprocess(self, data):
        assert len(self.context.request_ids.values()) <= 2
        prefill, decode = [], []
        for req_id, req_data in zip(self.context.request_ids.values(), data):
            if not req_id in self.context.cache:
                print(req_data["body"])
                data = json.loads(req_data["body"])
                encoded = self.tokenizer(data["prompt"], return_tensors='pt')
                encoded["past_key_values"] = None
                self.context.cache[req_id] = {
                    "stopping_criteria": self.create_stopping_criteria(req_id, max_new_tokens=data["max_new_tokens"]),
                    "encoded": encoded,
                    "prompt_length": len(encoded["input_ids"]),
                }
                prefill.append(req_id)
            else:
                decode.append(req_id)
        return prefill, decode

    def inference(self, *args):
        prefill, decode_ids = args[0]
        
        results = {}
        for req_id in prefill:
            results[req_id] = self.run_prefill(req_id)
        
        decode_result = self.run_decode(decode_ids) if decode_ids else {}
        results.update(decode_result)
        return [results[i] for i in self.context.request_ids.values()
        ]

    def postprocess(self, x):
        self.context.stopping_criteria = [
            self.context.cache[i]["stopping_criteria"]
            for i in self.context.request_ids.values()
        ]
        return x

    def create_stopping_criteria(self, req_id, max_new_tokens=25):
        class StoppingCriteria(object):
            def __init__(
                self, cache, req_id, stop_token, max_new_tokens, 
            ):
                self.req_id = req_id
                self.cache = cache
                self.max_new_tokens = max_new_tokens
                self.stop_token = stop_token

            def __call__(self, res):
                self.max_new_tokens -= 1
                
                if self.max_new_tokens == 0 or res["ids"][-1] == self.stop_token:
                    self.clean_up()
                    return True
                return False

            def clean_up(self):
                del self.cache[self.req_id]

        return StoppingCriteria(
            self.context.cache,
            req_id,
            self.tokenizer.eos_token_id,
            max_new_tokens,
        )
        
    def run_prefill(self, req_id):
        assert self.context.cache[req_id]["encoded"]["past_key_values"] is None
        output = self.model.generate(
            **self.context.cache[req_id]["encoded"],
            max_new_tokens=1,
            return_dict_in_generate=True,
            use_cache=True
            )
        self.context.cache[req_id]["encoded"]["past_key_values"] = self.context.kv_cache["past_key_values"]
        del self.context.kv_cache["past_key_values"]
        result = {
            "text": self.tokenizer.decode(output.sequences[0], skip_special_tokens=True),
            "ids": output.sequences[0].tolist(),
        }
        self.context.cache[req_id]["encoded"]["input_ids"] = output.sequences
        attention_mask = self.context.cache[req_id]["encoded"]["attention_mask"]
        attention_mask = torch.concat((attention_mask, torch.ones((1,1), dtype=torch.int64)), dim=1)
        self.context.cache[req_id]["encoded"]["attention_mask"] = attention_mask
        return result
        
    def run_decode(self, ids):
        assert len(ids)
        lengths = list(torch.sum(self.context.cache[i]["encoded"]["attention_mask"], dim=1).item() for i in ids)
        max_len = max(lengths)
        
        input_ids = []
        attention_mask = []
        kv_cache = {}
        for i, seq_len in zip(ids, lengths):
            input_ids.append(self.context.cache[i]["encoded"]["input_ids"])
            attention_mask.append(self.context.cache[i]["encoded"]["attention_mask"])
            
            for layer_idx, layer_kv in enumerate(self.context.cache[i]["encoded"]["past_key_values"]):
                k, v = layer_kv
                kv_cache[layer_idx] = kv_cache.get(layer_idx, {})
                kv_cache[layer_idx][0] = kv_cache.get(layer_idx, {}).get(0,[]) + [k]
                kv_cache[layer_idx][1] = kv_cache.get(layer_idx, {}).get(1,[]) + [v]
            padded_len = input_ids[-1].size()[-1]
            if padded_len < max_len:
                n = max_len - seq_len
                input_ids[-1] = torch.concat((self.tokenizer.pad_token_id + torch.zeros((1,n), dtype=torch.int64), input_ids[-1]), dim=1)
                attention_mask[-1] = torch.concat((torch.zeros((1,n), dtype=torch.int64), attention_mask[-1]), dim=1)
                
                size_delta = list(kv_cache[0][0][-1].size())
                size_delta[2] = n
                dtype = kv_cache[0][0][-1].dtype
                for layer_idx in range(len(kv_cache)):
                    kv_cache[layer_idx][0][-1] = torch.concat((torch.zeros(size_delta, dtype=dtype), kv_cache[layer_idx][0][-1]), dim=2)
                    kv_cache[layer_idx][1][-1] = torch.concat((torch.zeros(size_delta, dtype=dtype), kv_cache[layer_idx][1][-1]), dim=2)
                
            elif padded_len > max_len:
                input_ids[-1] = input_ids[-1][:, -max_len:]
                attention_mask[-1] = attention_mask[-1][:,-max_len:]
                
                for layer_idx in range(len(kv_cache)):
                    kv_cache[layer_idx][0][-1] = kv_cache[layer_idx][0][-1][:,:,(-max_len+1):,:]
                    kv_cache[layer_idx][1][-1] = kv_cache[layer_idx][1][-1][:,:,(-max_len+1):,:]
            del self.context.cache[i]["encoded"]["past_key_values"]
        
        for layer_idx in range(len(kv_cache)):
            kv_cache[layer_idx][0] = torch.concat(kv_cache[layer_idx][0], dim=0)
            kv_cache[layer_idx][1] = torch.concat(kv_cache[layer_idx][1], dim=0)
            
        kv_cache = tuple((kv_cache[layer_idx][0], kv_cache[layer_idx][1]) for layer_idx in range(len(kv_cache)))
            
        encoded = {
            "input_ids": torch.concat(input_ids, dim=0),
            "attention_mask": torch.concat(attention_mask, dim=0),
            "past_key_values": kv_cache,
        }
        
        outputs = self.model.generate(
            **encoded,
            max_new_tokens=1,
            return_dict_in_generate=True,
            use_cache=True
            )

        results = {}
        for idx, req_id in enumerate(ids):
            self.context.cache[req_id]["encoded"]["past_key_values"] = tuple(tuple(kv[idx,...].unsqueeze(0) for kv in layers) for layers in self.context.kv_cache["past_key_values"])
            self.context.cache[req_id]["encoded"]["input_ids"] = outputs.sequences[idx].unsqueeze(0)
            attention_mask = encoded["attention_mask"][idx].unsqueeze(0)
            attention_mask = torch.concat((attention_mask, torch.ones((1,1), dtype=torch.int64)), dim=1)
            self.context.cache[req_id]["encoded"]["attention_mask"] = attention_mask
            results[req_id] = {
                "text":self.tokenizer.decode(outputs.sequences[idx][-1], skip_special_tokens=True),
                "ids":[outputs.sequences[idx][-1].item()],
                }
        del self.context.kv_cache["past_key_values"]
        return results
