# P3b embedder test: CLIP vs EmbeddingGemma 2 on NOAA coral crops

This was a throwaway test run on 2026-10-13 to choose the default embedder for P3b mining. No code was kept. The numbers are recorded here so they outlast the session.

## Setup

- **Sample:** 3,000 crops from NOAA Pacific T3. That is 30 labels with 100 crops each, all included, unedited and with the source present. None were skipped.
- **Hardware:** a one-off container from the app image, CPU only, 8 cores, fp32, batch 16.
- **Metrics:**
  - throughput in crops per second;
  - leave-one-out kNN on cosine similarity, both as top-10 majority-vote label accuracy and as the fraction of the 10 neighbours that share the crop's label;
  - chance level is 3.3%.

## Results

| Embedder / config | Dims | Crops/s | kNN@10 accuracy | Same-label fraction | Time for 941,550 crops |
|---|---|---|---|---|---|
| CLIP (library: open_clip ViT-B-32, laion2b_s34b_b79k) | 512 | 59.9 | 16.3% | 9.3% | 4.4 h |
| EmbeddingGemma 2, 70 vision tokens | 768 | 1.87 | 20.5% | 12.0% | 5.8 days |
| EmbeddingGemma 2, 70 vision tokens | 256 | 1.87 | 19.7% | 11.9% | 5.8 days |
| EmbeddingGemma 2, 280 tokens (600-crop subset) | 768 | 0.31 | 16.8% | 9.2% | 35 days |
| EmbeddingGemma 2, 280 tokens (600-crop subset) | 256 | 0.31 | 15.2% | 8.8% | 35 days |

- **Accuracy gap:** on the common 600-crop subset, CLIP scores 13.0% and EmbeddingGemma 2 at 70 tokens scores 14.2%, a gap within noise.
- **Resources:** CLIP loads in 38 s and peaks at 1.8 GB of memory. EmbeddingGemma 2 loads in about 29 s and peaks at 3.1 GB (70 tokens) or 4.8 GB (280 tokens).
- **Versions:** sentence-transformers 6.1.0, transformers 5.19.0, torch 2.14.1+cpu, open_clip_torch 3.3.0. The install had no conflicts and the model needs no Hugging Face token.
- **Gotcha:** sentence-transformers loads EmbeddingGemma 2 in bf16 by default, which was about 4× slower on this CPU. Force `model_kwargs={"torch_dtype": torch.float32}`.

## Conclusions for P3b

1. **Default to CLIP.** EmbeddingGemma 2 separates labels only slightly better and is about 30× slower on CPU. Embedding the whole project with it would take about 6 days, against 4.4 hours for CLIP.
2. **Both general-purpose embedders are weak on fine coral classes.** About 16–20% kNN@10 accuracy across 30 labels. Two consequences:
   - The neighbour-label-disagreement signal for "likely mislabels" will be noisy with these embeddings. Treat it as a weak signal, behind CoralNet and evaluation-prediction disagreement.
   - P3b should consider features from the project's own coral classifier (a YOLO classify backbone) as a second, domain-specific embedding. Benchmark it the same way before relying on it.
3. **Uses where CLIP should do well:** near-duplicate and leakage detection, text search ("sand", "bleached") and "more like this" browsing. None of these need fine label separation.
