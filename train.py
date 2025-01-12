# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import os

def install_flash_attn():
    try:
        print("Installing flash attention!")
        os.system("pip install flash-attn --no-build-isolation --upgrade --quiet")
        print("Installing flash attention completed!")
    except Exception as exc:
        print("WARN: flash-attn failed to install. It is OK if you have not enabled flash-attention-2 option.")

# install_flash_attn()


import json
import shutil
from os.path import exists, join, isdir
from dataclasses import dataclass, field
from typing import Optional, Dict, Sequence, Any, List
import numpy as np
from datasets.formatting.formatting import LazyBatch
from tqdm import tqdm
import logging
import warnings
import bitsandbytes as bnb
import importlib
from packaging import version
import torch
import transformers
from torch.nn.utils.rnn import pad_sequence
import argparse
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    set_seed,
    Seq2SeqTrainer,
    BitsAndBytesConfig
)
from datasets import load_dataset
import evaluate

from peft import (
    prepare_model_for_kbit_training,
    LoraConfig,
    get_peft_model,
    PeftModel
)
from peft.tuners.lora import LoraLayer
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
from accelerate import Accelerator
from huggingface_hub import ModelCard

from utils import load_template

def is_ipex_available():
    def get_major_and_minor_from_version(full_version):
        return str(version.parse(full_version).major) + "." + str(version.parse(full_version).minor)

    _torch_version = importlib.metadata.version("torch")
    if importlib.util.find_spec("intel_extension_for_pytorch") is None:
        return False
    _ipex_version = "N/A"
    try:
        _ipex_version = importlib.metadata.version("intel_extension_for_pytorch")
    except importlib.metadata.PackageNotFoundError:
        return False
    torch_major_and_minor = get_major_and_minor_from_version(_torch_version)
    ipex_major_and_minor = get_major_and_minor_from_version(_ipex_version)
    if torch_major_and_minor != ipex_major_and_minor:
        warnings.warn(
            f"Intel Extension for PyTorch {ipex_major_and_minor} needs to work with PyTorch {ipex_major_and_minor}.*,"
            f" but PyTorch {_torch_version} is found. Please switch to the matching version and run again."
        )
        return False
    return True

if torch.cuda.is_available():
    torch.backends.cuda.matmul.allow_tf32 = True

logger = logging.getLogger(__name__)

IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
CONVERSATION_KEY = 'conversation'
DS_FULL_KEY='full'
DS_PROMPT_LEN_KEY='prompt_lens'

@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(
        default="TinyLlama/TinyLlama-1.1B-intermediate-step-1195k-token-2.5T",
    )
    trust_remote_code: Optional[bool] = field(
        default=False,
        metadata={"help": "Enable unpickling of arbitrary code in AutoModelForCausalLM#from_pretrained."}
    )
    use_auth_token: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables using Huggingface auth token from Git Credentials."}
    )

@dataclass
class DataArguments:
    eval_dataset_size: float = field(
        default=0.02, metadata={"help": "Size of validation dataset."}
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    model_max_len: int = field(
        default=4096,
        metadata={"help": "Maximum model length (input and output).  Sequences will be right padded (and possibly truncated)."},
    )
    skip_excess_length: bool = field(
        default=True,
        metadata={"help": "Purge dataset items that exceed model_max_len"}
    )
    dataset: str = field(
        default='habanoz/airoboros-3.1-no-mathjson-max-1k-chat-format',
        metadata={"help": "Which dataset to finetune on. See datamodule for options."}
    )
    include_sources: Optional[str] = field(
        default="ALL",
        metadata={"help": "Comma separated list of sources to include (source field in dataset)"}
    )

@dataclass
class TrainingArguments(transformers.Seq2SeqTrainingArguments):
    cache_dir: Optional[str] = field(
        default=None
    )
    train_on_source: Optional[bool] = field(
        default=False,
        metadata={"help": "Whether to train on the input in addition to the target text."}
    )
    mmlu_split: Optional[str] = field(
        default='eval',
        metadata={"help": "The MMLU split to run on"}
    )
    mmlu_dataset: Optional[str] = field(
        default='mmlu-fs',
        metadata={"help": "MMLU dataset to use: options are `mmlu-zs` for zero-shot or `mmlu-fs` for few shot."}
    )
    do_mmlu_eval: Optional[bool] = field(
        default=False,
        metadata={"help": "Whether to run the MMLU evaluation."}
    )
    max_mmlu_samples: Optional[int] = field(
        default=None,
        metadata={"help": "If set, only evaluates on `max_mmlu_samples` of the MMMLU dataset."}
    )
    mmlu_source_max_len: int = field(
        default=2048,
        metadata={"help": "Maximum source sequence length for mmlu."}
    )
    full_finetune: bool = field(
        default=False,
        metadata={"help": "Finetune the entire model without adapters."}
    )
    adam8bit: bool = field(
        default=False,
        metadata={"help": "Use 8-bit adam."}
    )
    double_quant: bool = field(
        default=True,
        metadata={"help": "Compress the quantization statistics through double quantization."}
    )
    quant_type: str = field(
        default="nf4",
        metadata={"help": "Quantization data type to use. Should be one of `fp4` or `nf4`."}
    )
    bits: int = field(
        default=4,
        metadata={"help": "How many bits to use."}
    )
    lora_r: int = field(
        default=64,
        metadata={"help": "Lora R dimension."}
    )
    lora_alpha: float = field(
        default=16,
        metadata={"help": " Lora alpha."}
    )
    lora_dropout: float = field(
        default=0.0,
        metadata={"help":"Lora dropout."}
    )
    max_memory_MB: int = field(
        default=80000,
        metadata={"help": "Free memory per gpu."}
    )
    report_to: str = field(
        default='tensorboard',
        metadata={"help": "To use wandb or something else for reporting."}
    )
    use_fast_tokenizer: bool = field(default=True, metadata={"help": "Use fast tokenizer"})
    pad_token: str = field(default=None, metadata={"help": "Custom pad token, e.g. for qwen"})
    eos_token: str = field(default=None, metadata={"help": "Custom EOS token, e.g. for qwen"})
    bos_token: str = field(default=None, metadata={"help": "Custom BOS token, e.g. for qwen"})
    unk_token: str = field(default=None, metadata={"help": "Custom UNK token, e.g. for qwen"})
    padding_side: str = field(default="right", metadata={"help": "tokenizer padding side"})
    final_output_dir: str = field(default='./final', metadata={"help": 'The final output directory, for completed model'})
    output_dir: str = field(default='./output', metadata={"help": 'The output (and intermediate) directory.'})
    optim: str = field(default='adamw_apex_fused', metadata={"help": 'The optimizer to be used'})
    per_device_train_batch_size: int = field(default=1, metadata={"help": 'The training batch size per GPU. Increase for better speed.'})
    per_device_eval_batch_size: int = field(default=1, metadata={"help": 'The eval batch size per GPU. Increase for better speed.'})
    gradient_accumulation_steps: int = field(default=16, metadata={"help": 'How many gradients to accumulate before to perform an optimizer step'})
    num_train_epochs: int = field(default=3, metadata={"help": 'Number of training epochs.'})
    weight_decay: float = field(default=0.0, metadata={"help": 'The L2 weight decay rate of AdamW'}) # use lora dropout instead for regularization if needed
    learning_rate: float = field(default=0.0002, metadata={"help": 'The learning rate'})
    remove_unused_columns: bool = field(default=False, metadata={"help": 'Removed unused columns. Needed to make this codebase work.'})
    max_grad_norm: float = field(default=0.3, metadata={"help": 'Gradient clipping max norm. This is tuned and works well for all models tested.'})
    gradient_checkpointing: bool = field(default=True, metadata={"help": 'Use gradient checkpointing. You want to use this.'})
    do_train: bool = field(default=True, metadata={"help": 'To train or not to train, that is the question?'})
    lr_scheduler_type: str = field(default='constant', metadata={"help": 'Learning rate schedule. Constant a bit better than cosine, and has advantage for analysis'})
    warmup_ratio: float = field(default=0.03, metadata={"help": 'Fraction of steps to do a warmup for'})
    logging_steps: int = field(default=10, metadata={"help": 'The frequency of update steps after which to log the loss'})
    group_by_length: bool = field(default=False, metadata={"help": 'Group sequences into batches with same length. Saves memory and speeds up training considerably.'})
    save_strategy: str = field(default='epoch', metadata={"help": 'When to save checkpoints'})
    save_steps: int = field(default=250, metadata={"help": 'How often to save a model'})
    save_total_limit: int = field(default=1, metadata={"help": 'How many checkpoints to save before the oldest is overwritten'})
    deepspeed: str = field(default=None, metadata={"help": "deepspeed configuration path"})
    using_fsdp: bool = field(default=False, metadata={"help": "Flag indicating whether or not you are using FSDP (via accelerate)"})
    max_shard_size: str = field(default="5GB", metadata={"help": "Max shard size when saving model after full finetune."})
    save_quantized_base: bool = field(default=False, metadata={"help": "Optionally save the quantized base model"})
    # attn_implementation: str = field(default=None, metadata={"help": "Attention implementation."})
    use_flash_attention_2: bool = field(default=False, metadata={"help": "Use flash attention 2."})
    neftune_noise_alpha: int = field(default=5, metadata={"help": "NEFTune noise alpha value"})

@dataclass
class GenerationArguments:
    # For more hyperparameters check:
    # https://huggingface.co/docs/transformers/main_classes/text_generation#transformers.GenerationConfig
    # Length arguments
    max_new_tokens: Optional[int] = field(
        default=256,
        metadata={"help": "Maximum number of new tokens to be generated in evaluation or prediction loops"
                          "if predict_with_generate is set."}
    )
    min_new_tokens : Optional[int] = field(
        default=None,
        metadata={"help": "Minimum number of new tokens to generate."}
    )

    # Generation strategy
    do_sample: Optional[bool] = field(default=False)
    num_beams: Optional[int] = field(default=1)
    num_beam_groups: Optional[int] = field(default=1)
    penalty_alpha: Optional[float] = field(default=None)
    use_cache: Optional[bool] = field(default=True)

    # Hyperparameters for logit manipulation
    temperature: Optional[float] = field(default=0.7)
    top_k: Optional[int] = field(default=50)
    top_p: Optional[float] = field(default=1.0)
    typical_p: Optional[float] = field(default=1.0)
    diversity_penalty: Optional[float] = field(default=0.0)
    repetition_penalty: Optional[float] = field(default=1.0)
    length_penalty: Optional[float] = field(default=1.0)
    no_repeat_ngram_size: Optional[int] = field(default=0)

def find_all_linear_names(args, model):
    cls = bnb.nn.Linear4bit if args.bits == 4 else (bnb.nn.Linear8bitLt if args.bits == 8 else torch.nn.Linear)
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])
    if 'lm_head' in lora_module_names: # needed for 16-bit
        lora_module_names.remove('lm_head')
    return list(lora_module_names)


class SavePeftModelCallback(transformers.TrainerCallback):
    def __init__(self, trainer, **_):
        self.trainer = trainer


    def save_model(self, args, state, kwargs):
        print('Saving PEFT checkpoint...')
        checkpoint_folder = os.path.join(args.output_dir, f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}")
        peft_model_path = os.path.join(checkpoint_folder, "adapter_model")

        if getattr(self.trainer, "deepspeed"):
            self.trainer.accelerator.wait_for_everyone()
            state_dict = self.trainer.accelerator.get_state_dict(self.trainer.deepspeed)
            unwrapped_model = self.trainer.accelerator.unwrap_model(self.trainer.deepspeed)
            if self.trainer.accelerator.is_main_process:
                unwrapped_model.save_pretrained(peft_model_path, state_dict=state_dict, safe_serialization=True)
            self.trainer.accelerator.wait_for_everyone()
        else:
            kwargs["model"].save_pretrained(peft_model_path, safe_serialization=True)

        pytorch_model_path = os.path.join(checkpoint_folder, "pytorch_model.bin")
        if os.path.exists(pytorch_model_path):
            os.remove(pytorch_model_path)
        try:
            if os.path.exists(os.path.join(checkpoint_folder, f'global_step{state.global_step}')):
                print(f'Cleaning up global_step{state.global_step}')
                shutil.rmtree(os.path.join(checkpoint_folder, f'global_step{state.global_step}'))
        except Exception as exc:
            print(f'Failed to clean up global_step{state.global_step}: {exc}')

    def on_save(self, args, state, control, **kwargs):
        self.save_model(args, state, kwargs)
        return control

    def on_train_end(self, args, state, control, **kwargs):
        def touch(fname, times=None):
            with open(fname, 'a'):
                os.utime(fname, times)
        self.save_model(args, state, kwargs)
        touch(join(args.output_dir, 'completed'))

def get_accelerate_model(args, checkpoint_dir):

    if torch.cuda.is_available():
        n_gpus = torch.cuda.device_count()
    if is_ipex_available() and torch.xpu.is_available():
        n_gpus = torch.xpu.device_count()

    if args.full_finetune:
        assert args.bits in [16, 32]

    # Tokenizer...
    extra_tokens = {}
    for key in ("pad_token", "eos_token", "bos_token", "unk_token"):
        value = getattr(args, key, None)
        if value:
            extra_tokens[key] = value
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        cache_dir=args.cache_dir,
        use_fast=args.use_fast_tokenizer,
        padding_side=args.padding_side,
        tokenizer_type='llama' if 'llama' in args.model_name_or_path else None, # Needed for HF name change
        trust_remote_code=args.trust_remote_code,
        **extra_tokens,
    )
    if not tokenizer.pad_token_id:
        tokenizer.pad_token_id = tokenizer.unk_token_id
        tokenizer.pad_token = tokenizer.unk_token

    # Ensure the model has the correct token IDs (qwen!!!)
    extra_model_args = {}
    for key in ("pad_token", "eos_token", "bos_token", "unk_token"):
        value = getattr(args, key, None)
        if value:
            extra_model_args[f"{key}_id"] = getattr(tokenizer, f"{key}_id")
    if "qwen" in args.model_name_or_path:
        extra_model_args["bf16"] = True
        extra_model_args["use_flash_attn"] = True

    load_template(tokenizer)

    # Model...
    print(f'loading base model {args.model_name_or_path}...')
    compute_dtype = (torch.float16 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32))
    bnb_config = None
    if not args.full_finetune and args.bits in (4, 8):
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=args.bits == 4,
            load_in_8bit=args.bits == 8,
            llm_int8_threshold=6.0,
            llm_int8_has_fp16_weight=False,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=args.double_quant,
            bnb_4bit_quant_type=args.quant_type,
        )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        cache_dir=args.cache_dir,
        load_in_4bit=args.bits == 4,
        load_in_8bit=args.bits == 8,
        quantization_config=bnb_config,
        torch_dtype=(torch.float32 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32)),
        trust_remote_code=args.trust_remote_code,
        #attn_implementation=args.attn_implementation,
        use_flash_attention_2=args.use_flash_attention_2,
        **extra_model_args,
    )
    if compute_dtype == torch.float16 and args.bits == 4:
        if torch.cuda.is_bf16_supported():
            print('='*80)
            print('Your GPU supports bfloat16, you can accelerate training with the argument --bf16')
            print('='*80)

    if compute_dtype == torch.float16 and (is_ipex_available() and torch.xpu.is_available()):
        compute_dtype = torch.bfloat16
        print('Intel XPU does not support float16 yet, so switching to bfloat16')

    setattr(model, 'model_parallel', True)
    setattr(model, 'is_parallelizable', True)

    model.config.torch_dtype=(torch.float32 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32))

    # Resize token embeddings, if necessary, to accomodate fast tokenizer with added tokens.
    if "qwen" not in args.model_name_or_path:
        num_new_tokens = len(tokenizer) - len(model.get_input_embeddings().weight.data)
        if num_new_tokens > 0:
            input_embeddings_data = model.get_input_embeddings().weight.data
            output_embeddings_data = model.get_output_embeddings().weight.data

            input_embeddings_avg = input_embeddings_data[:-num_new_tokens].mean(dim=0, keepdim=True)
            output_embeddings_avg = output_embeddings_data[:-num_new_tokens].mean(dim=0, keepdim=True)

            input_embeddings_data[-num_new_tokens:] = input_embeddings_avg
            output_embeddings_data[-num_new_tokens:] = output_embeddings_avg
            model.resize_token_embeddings(len(tokenizer))

    if not args.full_finetune and args.bits in (8, 4):
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=args.gradient_checkpointing)

    if args.gradient_checkpointing and hasattr(model, 'gradient_checkpointing_enable'):
        model.gradient_checkpointing_enable()

    for name, module in model.named_modules():
        if isinstance(module, LoraLayer):
            if args.bf16:
                module = module.to(torch.bfloat16)
        if 'norm' in name:
            module = module.to(torch.bfloat16 if args.bf16 else torch.float32)
        if 'lm_head' in name or 'embed_tokens' in name:
            if hasattr(module, 'weight'):
                if args.bf16 and module.weight.dtype == torch.float32:
                    module = module.to(torch.bfloat16)
    
    if not args.full_finetune:
        if checkpoint_dir is not None:
            print("Loading adapters from checkpoint.")
            model = PeftModel.from_pretrained(model, join(checkpoint_dir, 'adapter_model'), is_trainable=True)
        else:
            print(f'adding LoRA modules...')
            modules = find_all_linear_names(args, model)
            config = LoraConfig(
                r=args.lora_r,
                lora_alpha=args.lora_alpha,
                target_modules=modules,
                lora_dropout=args.lora_dropout,
                bias="none",
                task_type="CAUSAL_LM",
            )
            model.enable_input_require_grads()
            model = get_peft_model(model, config)
    if args.using_fsdp:
        accelerator = Accelerator()
        model = accelerator.prepare_model(model)
    return model, tokenizer

def print_trainable_parameters(args, model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    if args.bits == 4: trainable_params /= 2
    print(
        f"trainable params: {trainable_params} || "
        f"all params: {all_param} || "
        f"trainable: {100 * trainable_params / all_param}"
    )

@dataclass
class DataCollatorForCausalLM(object):
    tokenizer: transformers.PreTrainedTokenizer
    model_max_len: int
    train_on_source: bool
    predict_with_generate: bool

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        input_ids = [torch.tensor(example[DS_FULL_KEY]) for example in instances]
        labels = [input_id.clone() for input_id in input_ids]

        if not self.train_on_source:
            source_lens = [example[DS_PROMPT_LEN_KEY] for example in instances]
            for idx in range(len(labels)):
                labels[idx][:source_lens[idx]] = IGNORE_INDEX

        # Apply padding
        input_ids = pad_sequence(input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id)
        labels = pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX) if not self.predict_with_generate else None

        data_dict = {
            'input_ids': input_ids,
            'attention_mask': input_ids.ne(self.tokenizer.pad_token_id),
            'labels': labels
        }
        return data_dict

def extract_unnatural_instructions_data(examples, extract_reformulations=False):
    out = {
        'input': [],
        'output': [],
    }
    for example_instances in examples['instances']:
        for instance in example_instances:
            out['input'].append(instance['instruction_with_input'])
            out['output'].append(instance['output'])
    if extract_reformulations:
        for example_reformulations in examples['reformulations']:
            if example_reformulations is not None:
                for instance in example_reformulations:
                    out['input'].append(instance['instruction_with_input'])
                    out['output'].append(instance['output'])
    return out

def make_data_module(tokenizer: transformers.PreTrainedTokenizer, args) -> Dict:
    """
    Make dataset and collator for supervised fine-tuning.
    Datasets are expected to compatible with Huggingface chat templates.
    """
    # Load dataset.
    dataset = load_dataset(args.dataset)
    is_bos_present = _is_bos_present_in_template(tokenizer, dataset['train'][0][CONVERSATION_KEY])
    map_lamb = lambda x: _apply_and_tokenize_batches(tokenizer, args.model_max_len, x, add_special=not is_bos_present, train_on_source=args.train_on_source)
    dataset = dataset.map(map_lamb, batched=True, desc="Apply and Tokenize")

    # Split train/eval, reduce size
    if args.do_eval or args.do_predict:
        if 'eval' in dataset:
            eval_dataset = dataset['eval']
        elif 'test' in dataset:
            eval_dataset = dataset['test']
        else:
            print('Splitting train dataset in train and validation according to `eval_dataset_size`')
            if 'category' in dataset["train"].column_names:
                dataset["train"] = dataset["train"].class_encode_column('category')
                dataset = dataset["train"].train_test_split(
                    test_size=args.eval_dataset_size, stratify_by_column='category', seed=args.seed
                )
            else:
                dataset = dataset["train"].train_test_split(
                    test_size=args.eval_dataset_size, shuffle=True, seed=args.seed
                )
            eval_dataset = dataset['test']
        if args.max_eval_samples is not None and len(eval_dataset) > args.max_eval_samples:
            eval_dataset = eval_dataset.select(range(args.max_eval_samples))
        if args.group_by_length: # not supported. Let it fail for the time being...
            eval_dataset = eval_dataset.map(lambda x: {'length': len(x['input']) + len(x['output'])})
    if args.do_train:
        train_dataset = dataset['train']
        if args.max_train_samples is not None and len(train_dataset) > args.max_train_samples:
            train_dataset = train_dataset.select(range(args.max_train_samples))
        if args.group_by_length: # not supported. Let it fail for the time being...
            train_dataset = train_dataset.map(lambda x: {'length': len(x['input']) + len(x['output'])})

    # Remove any training data that exceeds the max length.
    if args.skip_excess_length:
        bos = tokenizer.bos_token if not is_bos_present else ''
        eos = tokenizer.eos_token if not is_bos_present else ''

        def _get_data_length(item):
            prompt = f"{bos}{item[DS_FULL_KEY]}{eos}"
            return len(
                tokenizer(
                    prompt,
                    max_length=args.model_max_len + 1,
                    truncation=True,
                    add_special_tokens=False
                ).input_ids
            )

        train_dataset = train_dataset.filter(
            lambda x: _get_data_length(x) < args.model_max_len - 10
        )

    if args.do_train:
        train_dataset = train_dataset.remove_columns(
            [col for col in train_dataset.column_names if col not in [DS_PROMPT_LEN_KEY, DS_FULL_KEY]]
        )
    if args.do_eval:
        eval_dataset = eval_dataset.remove_columns(
            [col for col in eval_dataset.column_names if col not in [DS_PROMPT_LEN_KEY, DS_FULL_KEY]]
        )

    data_collator = DataCollatorForCausalLM(
        tokenizer=tokenizer,
        model_max_len=args.model_max_len,
        train_on_source=args.train_on_source,
        predict_with_generate=args.predict_with_generate,
    )
    return dict(
        train_dataset=train_dataset if args.do_train else None,
        eval_dataset=eval_dataset if args.do_eval else None,
        predict_dataset=eval_dataset if args.do_predict else None,
        data_collator=data_collator
    )

def _is_bos_present_in_template(tokenizer, sample_conversation: List[Dict]):
    sample = tokenizer.apply_chat_template(sample_conversation, tokenize=False, add_generation_prompt=True)
    bos_token_present = sample.startswith(tokenizer.bos_token)
    return bos_token_present


def _apply_and_tokenize_batches(tokenizer, max_len, items, add_special, train_on_source=True):
    if type(items) != LazyBatch:
        raise ValueError("_apply_and_tokenize_batches should be used with batched map method! e.g. dataset.map(lambda x: _apply_and_tokenize_batches(tokenizer, x, True, True), batched=True)")

    bos = tokenizer.bos_token if add_special else ''
    eos = tokenizer.eos_token if add_special else ''

    str_list = []
    for item in items[CONVERSATION_KEY]:
        str_list.append(bos + tokenizer.apply_chat_template(item, tokenize=False, add_generation_prompt=False) + eos)

    full_input_ids_list = tokenize(tokenizer, max_len, str_list).input_ids

    columns = {
        DS_FULL_KEY: full_input_ids_list
    }

    if not train_on_source:
        str_src_list = []
        for item in items[CONVERSATION_KEY]:
            str_src_list.append(
                bos + tokenizer.apply_chat_template(item[:-1], tokenize=False, add_generation_prompt=True))

        conversation_src_input_ids = tokenize(tokenizer, max_len, str_src_list).input_ids
        conversation_src_input_id_lens = [len(ids) for ids in conversation_src_input_ids]
        columns[DS_PROMPT_LEN_KEY] = conversation_src_input_id_lens

    return columns

def tokenize(tokenizer, model_max_len, sequence):
    return tokenizer(
        sequence,
        max_length=model_max_len,
        truncation=True,
        add_special_tokens=False,
        padding=False
    )

def get_last_checkpoint(checkpoint_dir):
    if isdir(checkpoint_dir):
        is_completed = exists(join(checkpoint_dir, 'completed'))
        if is_completed: return None, True # already finished
        max_step = 0
        for filename in os.listdir(checkpoint_dir):
            if isdir(join(checkpoint_dir, filename)) and filename.startswith('checkpoint'):
                max_step = max(max_step, int(filename.replace('checkpoint-', '')))
        if max_step == 0: return None, is_completed # training started, but no checkpoint
        checkpoint_dir = join(checkpoint_dir, f'checkpoint-{max_step}')
        print(f"Found a previous checkpoint at: {checkpoint_dir}")
        return checkpoint_dir, is_completed # checkpoint found!
    return None, False # first training

def train():
    hfparser = transformers.HfArgumentParser((
        ModelArguments, DataArguments, TrainingArguments, GenerationArguments
    ))
    model_args, data_args, training_args, generation_args, extra_args = \
        hfparser.parse_args_into_dataclasses(return_remaining_strings=True)
    training_args.generation_config = transformers.GenerationConfig(**vars(generation_args))
    args = argparse.Namespace(
        **vars(model_args), **vars(data_args), **vars(training_args)
    )
    print(args)

    checkpoint_dir, completed_training = get_last_checkpoint(args.output_dir)
    if completed_training:
        print('Detected that training was already completed!')

    model, tokenizer = get_accelerate_model(args, checkpoint_dir)

    model.config.use_cache = False
    print('loaded model')
    set_seed(args.seed)

    data_module = make_data_module(tokenizer=tokenizer, args=args)

    training_args.neftune_noise_alpha = args.neftune_noise_alpha
    trainer = Seq2SeqTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        **{k:v for k,v in data_module.items() if k != 'predict_dataset'},
    )

    # Callbacks
    if not args.full_finetune:
        trainer.add_callback(SavePeftModelCallback(trainer))
    if args.do_mmlu_eval:
        if args.mmlu_dataset == 'mmlu-zs':
            mmlu_dataset = load_dataset("json", data_files={
                'eval': 'data/mmlu/zero_shot_mmlu_val.json',
                'test': 'data/mmlu/zero_shot_mmlu_test.json',
            })
            mmlu_dataset = mmlu_dataset.remove_columns('subject')
        # MMLU Five-shot (Eval/Test only)
        elif args.mmlu_dataset == 'mmlu' or args.mmlu_dataset == 'mmlu-fs':
            mmlu_dataset = load_dataset("json", data_files={
                'eval': 'data/mmlu/five_shot_mmlu_val.json',
                'test': 'data/mmlu/five_shot_mmlu_test.json',
            })
            # mmlu_dataset = mmlu_dataset.remove_columns('subject')
        mmlu_dataset = mmlu_dataset[args.mmlu_split]
        if args.max_mmlu_samples is not None:
            mmlu_dataset = mmlu_dataset.select(range(args.max_mmlu_samples))
        abcd_idx = [
            tokenizer("A", add_special_tokens=False).input_ids[0],
            tokenizer("B", add_special_tokens=False).input_ids[0],
            tokenizer("C", add_special_tokens=False).input_ids[0],
            tokenizer("D", add_special_tokens=False).input_ids[0],
        ]
        accuracy = evaluate.load("accuracy")

        class MMLUEvalCallback(transformers.TrainerCallback):
            def on_evaluate(self, args, state, control, model, **kwargs):
                data_loader = trainer.get_eval_dataloader(mmlu_dataset)
                model_max_len = trainer.data_collator.model_max_len
                trainer.data_collator.model_max_len = args.mmlu_source_max_len
                trainer.model.eval()
                preds, refs = [], []
                loss_mmlu = 0
                for batch in tqdm(data_loader, total=len(data_loader)):
                    (loss, logits, labels) = trainer.prediction_step(trainer.model,batch,prediction_loss_only=False,)
                    # There are two tokens, the output, and eos token.
                    for i, logit in enumerate(logits):
                        label_non_zero_id = (batch['labels'][i] != -100).nonzero()[0][0]
                        logit_abcd = logit[label_non_zero_id-1][abcd_idx]
                        preds.append(torch.argmax(logit_abcd).item())
                    labels = labels[labels != IGNORE_INDEX].view(-1, 2)[:,0]
                    refs += [abcd_idx.index(label) for label in labels.tolist()]
                    loss_mmlu += loss.item()
                # Extract results by subject.
                results = {'mmlu_loss':loss_mmlu/len(data_loader)}
                subject = mmlu_dataset['subject']
                subjects = {s:{'refs':[], 'preds':[]} for s in set(subject)}
                for s,p,r in zip(subject, preds, refs):
                    subjects[s]['preds'].append(p)
                    subjects[s]['refs'].append(r)
                subject_scores = []
                for subject in subjects:
                    subject_score = accuracy.compute(
                        references=subjects[subject]['refs'],
                        predictions=subjects[subject]['preds']
                    )['accuracy']
                    results[f'mmlu_{args.mmlu_split}_accuracy_{subject}'] = subject_score
                    subject_scores.append(subject_score)
                results[f'mmlu_{args.mmlu_split}_accuracy'] = np.mean(subject_scores)
                trainer.log(results)
                trainer.data_collator.model_max_len = model_max_len

        trainer.add_callback(MMLUEvalCallback)

    # Verifying the datatypes and parameter counts before training.
    if not args.deepspeed:
        print_trainable_parameters(args, model)
    if not args.full_finetune:
        dtypes = {}
        for _, p in model.named_parameters():
            dtype = p.dtype
            if dtype not in dtypes: dtypes[dtype] = 0
            dtypes[dtype] += p.numel()
        total = 0
        for k, v in dtypes.items(): total += v
        for k, v in dtypes.items():
            print(k, v, v/total)

    all_metrics = {"run_name": args.run_name}
    # Training
    if args.do_train:
        logger.info("*** Train ***")
        # Note: `resume_from_checkpoint` not supported for adapter checkpoints by HF.
        # Currently adapter checkpoint is reloaded as expected but optimizer/scheduler states are not.
        train_result = trainer.train()
        metrics = train_result.metrics
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()
        all_metrics.update(metrics)
        trainer.save_model(args.output_dir)
    # Evaluation
    if args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate(metric_key_prefix="eval")
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)
        all_metrics.update(metrics)
    # Prediction
    if args.do_predict:
        logger.info("*** Predict ***")
        prediction_output = trainer.predict(test_dataset=data_module['predict_dataset'],metric_key_prefix="predict")
        prediction_metrics = prediction_output.metrics
        predictions = prediction_output.predictions
        predictions = np.where(predictions != -100, predictions, tokenizer.pad_token_id)
        predictions = tokenizer.batch_decode(
            predictions, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        os.makedirs(args.output_dir, exist_ok=True)
        with open(os.path.join(args.output_dir, 'predictions.jsonl'), 'w') as fout:
            for i, example in enumerate(data_module['predict_dataset']):
                example['prediction_with_input'] = predictions[i].strip()
                example['prediction'] = predictions[i].replace(example['input'], '').strip()
                fout.write(json.dumps(example) + '\n')
        print(prediction_metrics)
        trainer.log_metrics("predict", prediction_metrics)
        trainer.save_metrics("predict", prediction_metrics)
        all_metrics.update(prediction_metrics)

    if (args.do_train or args.do_eval or args.do_predict):
        os.makedirs(args.output_dir, exist_ok=True)
        with open(os.path.join(args.output_dir, "metrics.json"), "w") as fout:
            fout.write(json.dumps(all_metrics))

    if args.do_train or args.do_eval:
        if args.do_train:
            # Save training arguements
            with open(f"{args.output_dir}/training_args.json", 'w') as f:
                args_dict = vars(args)
                args_dict.pop('generation_config')
                args_dict.pop('distributed_state')
                args_dict.pop('__cached__setup_devices')
                json.dump(args_dict, f, indent=4)

        # add specify dataset name add eval loss.
        trainer.push_to_hub(commit_message="Model card update.", dataset=args.dataset)


    # Safely save final full-tune model.
    if args.full_finetune:
        trainer.accelerator.wait_for_everyone()
        state_dict = trainer.accelerator.get_state_dict(trainer.deepspeed)
        unwrapped_model = trainer.accelerator.unwrap_model(trainer.deepspeed)
        if trainer.accelerator.is_main_process:
            unwrapped_model.save_pretrained(args.final_output_dir, state_dict=state_dict, max_shard_size=args.max_shard_size)
            with open(os.path.join(args.final_output_dir, "config.json")) as infile:
                config = json.loads(infile.read())
            config["_name_or_path"] = os.path.basename(args.final_output_dir)
            with open(os.path.join(args.final_output_dir, "config.json"), "w") as outfile:
                outfile.write(json.dumps(config, indent=2))
            tokenizer.save_pretrained(args.final_output_dir)
        trainer.accelerator.wait_for_everyone()
    else:
        if args.deepspeed:
            trainer.accelerator.wait_for_everyone()
            state_dict = trainer.accelerator.get_state_dict(trainer.deepspeed)
            unwrapped_model = trainer.accelerator.unwrap_model(trainer.deepspeed)
            if trainer.accelerator.is_main_process:
                unwrapped_model.save_pretrained(args.final_output_dir, safe_serialization=True, state_dict=state_dict)
            trainer.accelerator.wait_for_everyone()
        else:
            trainer.accelerator.wait_for_everyone()
            if trainer.accelerator.is_main_process:
                trainer.model.save_pretrained(args.final_output_dir, safe_serialization=True)
            trainer.accelerator.wait_for_everyone()


if __name__ == "__main__":
    train()
