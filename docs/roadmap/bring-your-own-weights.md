# Serve your own fine-tuned weights from S3 (bring-your-own-weights)

**Status**: **Not delivered** — removed the half-wired `modelSource` field · **Updated**: 2026-07-16
**Priority**: Medium — a common ask for teams with private fine-tuned checkpoints
**Date added**: 2026-07-16

## Goal

Let a team upload fine-tuned model weights to the platform's S3 model-cache bucket
and serve them directly — without publishing the model to HuggingFace — by pointing
a serving CR at an S3 prefix (a `modelSource` field).

## Why it's not shipped

A `modelSource` field existed on the serving RGDs but was **not functional** and was
removed to keep the platform honest:

- On `VLLMEndpoint` it was schema-only — never referenced by the initContainer or the
  vLLM args.
- On `LLMDEndpoint` / `LLMDDisaggEndpoint` it was half-wired: vLLM was told to serve
  `/hf-cache/finetuned` when set, but the initContainer only ever synced the
  HuggingFace cache prefix (`s3://<bucket>/hf/<model-id>/`) — it never populated
  `/hf-cache/finetuned`, so a `modelSource` deploy would start vLLM against an empty
  directory and fail. It was never tested end-to-end.

## What exists today (and works)

The S3 model-cache bucket is a **cold-start accelerator for HuggingFace models**: the
serving initContainer syncs pre-seeded weights from `s3://<bucket>/hf/<model-id>/` to
the local HF cache (falling back to a HuggingFace download on a miss), and the
serving pods have read-only S3 access to it via IRSA (`inference-worker`). To serve a
fine-tuned model *today*, publish it to HuggingFace (public, or private + a token)
and reference it by ID — that path works like any other model.

## What it would take

1. Make the initContainer conditional: when `modelSource` is set, `PREFIX=<modelSource>`,
   `LOCAL=/hf-cache/finetuned`, and `s5cmd sync s3://<bucket>/<modelSource>/* → $LOCAL`.
2. Wire the vLLM arg to serve `/hf-cache/finetuned` when `modelSource` is set — in
   **all three** RGDs (VLLMEndpoint included), consistently.
3. Add the field back to the schemas + templates, and document the exact S3 layout
   (HF-format directory), how to discover the bucket name, and the upload command.
4. **Test end-to-end** on a real GPU with real weights (weights load + serve + a
   completion) before claiming it — the reason it was removed is it was never proven.

## Interim honesty

Docs describe only the real feature (HF-model cold-start cache). "Serve your own S3
weights via `modelSource`" is not claimed until 1–4 above ship and pass a live test.
