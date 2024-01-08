# Overview

This is a fork of :
- [artidoro/qlora](https://github.com/artidoro/qlora)
- [jondurbin/qlora](https://github.com/jondurbin/qlora)

## Differences from original

### HuggingFace Chat Templates

Dataset format option is removed. Only datasets that are compatible with Huggingface chat templates are accepted. For more info about chat templates see [Chat Templates](https://huggingface.co/docs/transformers/chat_templating)

### epochs instead of steps

This modification comes from jondurbin/qlora. `--max_steps` parameter is removed in favor of `--num_train_epochs` (defaults to 3)

## Full, non-(q)lora fine-tune example

Example used for the llama-2 7b airoboros, version 3.0:
```bash
export BASE_DIR=/workspace
export WANDB_API_KEY=[redacted]
export WANDB_PROJECT=airoboros-l2-7b-3.0

torchrun --nnodes=1 --nproc_per_node=7 $BASE_DIR/qlora/train.py \
  --model_name_or_path $BASE_DIR/llama-2-7b-hf \
  --working_dir $BASE_DIR/$WANDB_PROJECT-checkpoints \
  --output_dir $BASE_DIR/$WANDB_PROJECT \
  --num_train_epochs 5 \
  --logging_steps 1 \
  --save_strategy steps \
  --save_steps 15 \
  --save_total_limit 1 \
  --data_seed 11422 \
  --evaluation_strategy steps \
  --eval_dataset_size 0.02 \
  --eval_steps 5 \
  --max_new_tokens 4096 \
  --dataloader_num_workers 3 \
  --logging_strategy steps \
  --optim adamw_torch \
  --do_train \
  --full_finetune \
  --bits 16 \
  --bf16 \
  --dataset $BASE_DIR/conversations.json \
  --dataset_format airoboros_chat \
  --model_max_len 4096 \
  --per_device_train_batch_size 12 \
  --learning_rate 2e-5 \
  --lr_scheduler_type cosine \
  --warmup_ratio 0.005 \
  --weight_decay 0.0 \
  --seed 11422 \
  --report_to wandb \
  --deepspeed deepspeed-7b.json \
  --gradient_checkpointing \
  --use_flash_attention_2
```

`deepspeed-7b.json`

```json
{
  "gradient_accumulation_steps": "auto",
  "gradient_clipping": "auto",
  "train_batch_size": "auto",
  "train_micro_batch_size_per_gpu": "auto",
  "bf16": {
    "enabled": true
  },
  "zero_optimization": {
    "stage": 2,
    "contiguous_gradients": true,
    "overlap_comm": true,
    "reduce_scatter": true,
    "reduce_bucket_size": 5e8,
    "allgather_bucket_size": 5e8
  }
}
```

## QLoRA example

Script used for llama-2 70b airoboros, version 3.0:

```bash
export BASE_DIR=/workspace
export WANDB_API_KEY=[redacted]
export WANDB_PROJECT=airoboros-l2-70b-3.0

accelerate launch $BASE_DIR/qlora/train.py \
  --model_name_or_path $BASE_DIR/llama-2-70b-hf \
  --working_dir $BASE_DIR/$WANDB_PROJECT-checkpoints \
  --output_dir $BASE_DIR/$WANDB_PROJECT-peft \
  --merged_output_dir $BASE_DIR/$WANDB_PROJECT \
  --num_train_epochs 5 \
  --logging_steps 1 \
  --save_strategy steps \
  --save_steps 75 \
  --save_total_limit 3 \
  --data_seed 11422 \
  --evaluation_strategy steps \
  --per_device_eval_batch_size 2 \
  --eval_dataset_size 0.01 \
  --eval_steps 75 \
  --max_new_tokens 4096 \
  --dataloader_num_workers 3 \
  --logging_strategy steps \
  --do_train \
  --lora_r 64 \
  --lora_alpha 16 \
  --lora_modules all \
  --bf16 \
  --bits 4 \
  --double_quant \
  --quant_type nf4 \
  --lr_scheduler_type constant \
  --dataset $BASE_DIR/conversations.json \
  --dataset_format airoboros_chat \
  --model_max_len 4096 \
  --per_device_train_batch_size 2 \
  --learning_rate 0.00008 \
  --adam_beta2 0.999 \
  --max_grad_norm 0.3 \
  --lora_dropout 0.0 \
  --weight_decay 0.0 \
  --seed 11422 \
  --report_to wandb \
  --gradient_checkpointing \
  --use_flash_attention_2 \
  --ddp_find_unused_parameters False
```
