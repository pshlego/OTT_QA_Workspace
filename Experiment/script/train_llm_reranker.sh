export CUDA_VISIBLE_DEVICES=0,1,2,3
torchrun --nproc_per_node 4 \
-m FlagEmbedding.llm_reranker.finetune_for_layerwise.run \
--output_dir /mnt/sdd/shpark/reranker/table-to-passage-reranker-baai-15-negatives-128 \
--model_name_or_path BAAI/bge-reranker-v2-minicpm-layerwise \
--train_data /mnt/sdf/OTT-QAMountSpace/Dataset/Ours/Training_Dataset/table_to_passage/reranking_edge_15_negatives.jsonl \
--learning_rate 2e-4 \
--num_train_epochs 2 \
--per_device_train_batch_size 1 \
--gradient_accumulation_steps 32 \
--dataloader_drop_last True \
--query_max_len 512 \
--passage_max_len 512 \
--train_group_size 16 \
--logging_steps 1 \
--save_steps 50 \
--save_total_limit 50 \
--ddp_find_unused_parameters False \
--gradient_checkpointing \
--deepspeed /mnt/sdf/OTT-QAMountSpace/ModelCheckpoints/Ours/stage1.json \
--warmup_ratio 0.1 \
--bf16 \
--use_lora True \
--lora_rank 32 \
--lora_alpha 64 \
--use_flash_attn True \
--target_modules q_proj k_proj v_proj o_proj \
--start_layer 8 \
--head_multi True \
--head_type simple \
--lora_extra_parameters linear_head \
--finetune_type from_finetuned_model