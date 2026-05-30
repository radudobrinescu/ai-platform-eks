# Unsloth fine-tuning trainer image.
#
# Unsloth publishes no official Docker image (only community Dockerfiles), so we
# bake our own — pinned and mirrored to private ECR by Terraform
# (terraform/30.eks/30.cluster/unsloth-image.tf). The same cold-start
# optimization (EBS snapshot + SOCI) that handles the Ray LLM image handles this
# one, since it's the same order of magnitude in size.
#
# Pinned to a specific NVIDIA PyTorch base — deterministic, and it already ships
# cuDNN, FlashAttention, and Triton, saving ~3 GB and ~5 min vs. a bare base.
FROM nvcr.io/nvidia/pytorch:24.10-py3

# Unsloth + TRL pinned. transformers/peft/accelerate are constrained by Unsloth
# itself — don't override them or QLoRA breaks in subtle ways.
RUN pip install --no-cache-dir \
        "unsloth==2026.5.8" \
        "trl==0.21.0" \
        "datasets>=2.14.0" \
        "wandb>=0.16.0" \
        "huggingface_hub[hf_transfer]>=0.20.0" \
    && pip cache purge

# s5cmd for fast S3 transfers (same tool the InferenceEndpoint init-container uses).
ARG S5CMD_VERSION=2.3.0
RUN curl -sSL "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-64bit.tar.gz" \
    | tar -xzC /usr/local/bin s5cmd

# kubectl for the autoDeploy step — at the end of training the Job applies an
# InferenceEndpoint so the fine-tuned model never starts before the weights exist.
ARG KUBECTL_VERSION=v1.30.5
RUN curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# hf_transfer for faster HuggingFace downloads (fallback path on S3 cache miss).
ENV HF_HUB_ENABLE_HF_TRANSFER=1

WORKDIR /workspace
