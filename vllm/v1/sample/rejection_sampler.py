# SPDX-License-Identifier: Apache-2.0
from typing import Optional

import torch
import torch.nn as nn
import triton
import triton.language as tl

from vllm.logger import init_logger
from vllm.utils import is_pin_memory_available
from vllm.v1.sample.metadata import SamplingMetadata

logger = init_logger(__name__)
PLACEHOLDER_TOKEN_ID: tl.constexpr = -1
GREEDY_TEMPERATURE: tl.constexpr = -1


class RejectionSampler(nn.Module):

    def __init__(self, max_num_tokens: int = 32 * 1024):
        super().__init__()
        self.max_num_tokens = max_num_tokens
        self.buffer = torch.empty(
            max_num_tokens,
            dtype=torch.int64,
            device="cpu",
            pin_memory=is_pin_memory_available(),
        )
        self.buffer_np = self.buffer.numpy()

    def forward(
        self,
        draft_token_ids: list[list[int]],
        # [batch_size]
        cu_num_draft_tokens: torch.Tensor,
        # [num_tokens, vocab_size]
        draft_probs: Optional[torch.Tensor],
        # [num_tokens, vocab_size]
        target_logits: torch.Tensor,
        # [batch_size]
        bonus_token_ids: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> torch.Tensor:
        num_draft_tokens = [len(ids) for ids in draft_token_ids]
        max_spec_len = max(num_draft_tokens)
        # [num_tokens, vocab_size]
        target_probs = compute_probs(
            target_logits,
            sampling_metadata.temperature,
            cu_num_draft_tokens,
            max_spec_len,
        )
        # [batch_size, max_spec_len]
        draft_token_ids_tensor = self._async_copy_to_device(
            draft_token_ids,
            target_logits.device,
        )
        output_token_ids = rejection_sample(
            draft_token_ids_tensor,
            num_draft_tokens,
            cu_num_draft_tokens,
            draft_probs,
            target_probs,
            bonus_token_ids,
            sampling_metadata,
        )
        return output_token_ids

    def _async_copy_to_device(
        self,
        draft_token_ids: list[list[int]],
        device: torch.device,
    ) -> torch.Tensor:
        batch_size = len(draft_token_ids)
        num_draft_tokens = [len(ids) for ids in draft_token_ids]
        max_spec_len = max(num_draft_tokens)
        assert batch_size * max_spec_len <= self.max_num_tokens

        draft_token_ids_np = self.buffer_np[:batch_size * max_spec_len]
        draft_token_ids_np.fill(PLACEHOLDER_TOKEN_ID)
        for i, token_ids in enumerate(draft_token_ids):
            start = i * max_spec_len
            end = start + len(token_ids)
            draft_token_ids_np[start:end] = token_ids
        draft_token_ids_cpu = self.buffer[:batch_size * max_spec_len]
        draft_token_ids_cpu = draft_token_ids_cpu.view(batch_size,
                                                       max_spec_len)
        return draft_token_ids_cpu.to(device=device, non_blocking=True)

    @staticmethod
    def parse_output(output_token_ids: torch.Tensor) -> list[list[int]]:
        output_token_ids = output_token_ids.tolist()
        outputs: list[list[int]] = [[] for _ in output_token_ids]
        for i, token_ids in enumerate(output_token_ids):
            for token_id in token_ids:
                if token_id == PLACEHOLDER_TOKEN_ID:
                    break
                outputs[i].append(token_id)
        return outputs


def rejection_sample(
    # [batch_size, max_spec_len]
    draft_token_ids: torch.Tensor,
    # [batch_size]
    num_draft_tokens: list[int],
    # [batch_size]
    cu_num_draft_tokens: torch.Tensor,
    # [batch_size, max_spec_len, vocab_size]
    draft_probs: Optional[torch.Tensor],
    # [batch_size, max_spec_len, vocab_size]
    target_probs: torch.Tensor,
    # [batch_size]
    bonus_token_ids: torch.Tensor,
    sampling_metadata: SamplingMetadata,
) -> torch.Tensor:
    batch_size = draft_token_ids.shape[0]
    max_spec_len = draft_token_ids.shape[1]
    vocab_size = target_probs.shape[-1]
    device = target_probs.device
    assert draft_token_ids.is_contiguous()
    assert draft_probs is None or draft_probs.is_contiguous()
    assert target_probs.is_contiguous()
    assert bonus_token_ids.is_contiguous()

    # Rejection sampling.
    output_token_ids = torch.empty(
        (batch_size, max_spec_len + 1),
        dtype=torch.int64,
        device=device,
    )
    output_token_ids.fill_(PLACEHOLDER_TOKEN_ID)
    is_greedy = sampling_metadata.temperature == GREEDY_TEMPERATURE
    if not sampling_metadata.all_random:
        # Rejection sampling for greedy sampling requests.
        target_argmax = target_probs.argmax(dim=-1)
        rejection_greedy_sample_kernel[(batch_size, )](
            output_token_ids,
            draft_token_ids,
            target_argmax,
            bonus_token_ids,
            is_greedy,
            max_spec_len,
        )
        if sampling_metadata.all_greedy:
            return output_token_ids

    # Generate uniform probabilities for rejection sampling.
    uniform_probs = torch.rand(
        (batch_size, max_spec_len),
        dtype=torch.float32,
        device=device,
    )
    for i, generator in sampling_metadata.generators.items():
        num_tokens = num_draft_tokens[i]
        if num_tokens > 0:
            # NOTE(woosuk): We shouldn't use max_spec_len here because
            # max_spec_len is affected by other requests in the batch.
            uniform_probs[i][:num_tokens].uniform_(generator=generator)

    # Sample recovered tokens for each position.
    # Compute the adjusted probabilities.
    is_ngram = draft_probs is None
    if is_ngram:
        # [batch_size, max_spec_len, vocab_size]
        probs = target_probs.clone()
        # [batch_size, max_spec_len]
        safe_draft_token_ids = torch.where(
            draft_token_ids == PLACEHOLDER_TOKEN_ID, 0, draft_token_ids)
        # Set probabilities to 0 for draft token positions
        probs.scatter_(2, safe_draft_token_ids.unsqueeze(-1), 0)
    else:
        probs = torch.clamp(target_probs - draft_probs,
                            min=torch.finfo(torch.float32).tiny)
    probs /= probs.sum(dim=-1, keepdim=True)

    # NOTE(woosuk): Create only one distribution for each request.
    q = torch.empty(
        (batch_size, vocab_size),
        dtype=torch.float32,
        device=device,
    )
    q.exponential_()
    for i, generator in sampling_metadata.generators.items():
        if num_draft_tokens[i] > 0:
            q[i].exponential_(generator=generator)
    q = q.unsqueeze(dim=1)
    recovered_token_ids = probs.div_(q).argmax(dim=-1)
    recovered_token_ids = recovered_token_ids.view(batch_size, max_spec_len)

    # Rejection sampling for random sampling requests.
    rejection_random_sample_kernel[(batch_size, )](
        output_token_ids,
        draft_token_ids,
        draft_probs,
        target_probs,
        bonus_token_ids,
        recovered_token_ids,
        uniform_probs,
        is_greedy,
        max_spec_len,
        vocab_size,
        is_ngram,
    )
    return output_token_ids


@triton.jit
def rejection_random_sample_kernel(
    output_token_ids_ptr,  # [batch_size, max_spec_len + 1]
    draft_token_ids_ptr,  # [batch_size, max_spec_len]
    draft_probs_ptr,  # [batch_size, max_spec_len, vocab_size] or None
    target_probs_ptr,  # [batch_size, max_spec_len, vocab_size]
    bonus_token_ids_ptr,  # [batch_size]
    recovered_token_ids_ptr,  # [batch_size, max_spec_len]
    uniform_probs_ptr,  # [batch_size, UNIFORM_PROBS_LEN]
    is_greedy_ptr,  # [batch_size]
    max_spec_len,
    vocab_size,
    IS_NGRAM: tl.constexpr,
):
    seq_idx = tl.program_id(0)
    is_greedy = tl.load(is_greedy_ptr + seq_idx)
    if is_greedy:
        # Early exit for greedy sampling requests.
        return

    rejected = False
    finished = False
    num_generated = 0
    for pos in range(max_spec_len):
        if not finished:
            token_id = tl.load(draft_token_ids_ptr + seq_idx * max_spec_len +
                               pos)
            if token_id == PLACEHOLDER_TOKEN_ID:
                finished = True
            else:
                if IS_NGRAM:
                    draft_prob = 1
                else:
                    # NOTE(woosuk): Here, we assume that draft_prob is nonzero.
                    draft_prob = tl.load(draft_probs_ptr +
                                         seq_idx * max_spec_len * vocab_size +
                                         pos * vocab_size + token_id)
                target_prob = tl.load(target_probs_ptr +
                                      seq_idx * max_spec_len * vocab_size +
                                      pos * vocab_size + token_id)
                uniform_prob = tl.load(uniform_probs_ptr +
                                       seq_idx * max_spec_len + pos)
                if target_prob / draft_prob >= uniform_prob:
                    # Accept.
                    tl.store(
                        output_token_ids_ptr + seq_idx * (max_spec_len + 1) +
                        pos, token_id)
                    num_generated += 1
                else:
                    # Reject. Use recovered token.
                    rejected = True
                    recovered_token_id = tl.load(recovered_token_ids_ptr +
                                                 seq_idx * max_spec_len + pos)
                    tl.store(
                        output_token_ids_ptr + seq_idx * (max_spec_len + 1) +
                        pos, recovered_token_id)
                    num_generated += 1
                    finished = True

    if not rejected:
        # If all tokens are accepted, append the bonus token.
        bonus_token_id = tl.load(bonus_token_ids_ptr + seq_idx)
        tl.store(
            output_token_ids_ptr + seq_idx * (max_spec_len + 1) +
            num_generated, bonus_token_id)


@triton.jit
def rejection_greedy_sample_kernel(
    output_token_ids_ptr,  # [batch_size, max_spec_len + 1]
    draft_token_ids_ptr,  # [batch_size, max_spec_len]
    target_argmax_ptr,  # [batch_size, max_spec_len]
    bonus_token_ids_ptr,  # [batch_size]
    is_greedy_ptr,  # [batch_size]
    max_spec_len,
):
    seq_idx = tl.program_id(0)
    is_greedy = tl.load(is_greedy_ptr + seq_idx)
    if not is_greedy:
        # Early exit for non-greedy sampling requests.
        return

    rejected = False
    finished = False
    num_generated = 0
    for pos in range(max_spec_len):
        if not finished:
            token_id = tl.load(draft_token_ids_ptr + seq_idx * max_spec_len +
                               pos)
            if token_id == PLACEHOLDER_TOKEN_ID:
                finished = True
            else:
                draft_token_id = tl.load(draft_token_ids_ptr +
                                         seq_idx * max_spec_len + pos)
                target_argmax = tl.load(target_argmax_ptr +
                                        seq_idx * max_spec_len + pos)
                if draft_token_id == target_argmax:
                    # Accept.
                    tl.store(
                        output_token_ids_ptr + seq_idx * (max_spec_len + 1) +
                        pos, draft_token_id)
                    num_generated += 1
                else:
                    # Reject.
                    rejected = True
                    tl.store(
                        output_token_ids_ptr + seq_idx * (max_spec_len + 1) +
                        pos, target_argmax)
                    num_generated += 1
                    finished = True

    if not rejected:
        # If all tokens are accepted, append the bonus token.
        bonus_token_id = tl.load(bonus_token_ids_ptr + seq_idx)
        tl.store(
            output_token_ids_ptr + seq_idx * (max_spec_len + 1) +
            num_generated, bonus_token_id)


def compute_probs(
    logits: torch.Tensor,  # [num_tokens, vocab_size]
    temperature: torch.Tensor,  # [batch_size]
    cu_num_draft_tokens: torch.Tensor,  # [batch_size]
    max_spec_len: int,
) -> torch.Tensor:
    output_prob = torch.empty_like(logits, dtype=torch.float32)
    batch_size = temperature.shape[0]
    vocab_size = logits.shape[-1]
    compute_probs_kernel[(batch_size, max_spec_len)](
        output_prob,
        logits,
        temperature,
        cu_num_draft_tokens,
        vocab_size,
        triton.next_power_of_two(vocab_size),
    )
    return output_prob


@triton.jit
def compute_probs_kernel(
    output_prob_ptr,  # [num_tokens, vocab_size]
    logits_ptr,  # [num_tokens, vocab_size]
    temperature_ptr,  # [batch_size]
    cu_num_draft_tokens_ptr,  # [batch_size]
    vocab_size,
    PADDED_VOCAB_SIZE: tl.constexpr,
):
    req_idx = tl.program_id(0)
    if req_idx == 0:
        start_idx = 0
    else:
        start_idx = tl.load(cu_num_draft_tokens_ptr + req_idx - 1)
    end_idx = tl.load(cu_num_draft_tokens_ptr + req_idx)

    # Early exit for out-of-range positions.
    pos = tl.program_id(1)
    if pos >= end_idx - start_idx:
        return

    vocab_offset = tl.arange(0, PADDED_VOCAB_SIZE)
    logits = tl.load(logits_ptr + (start_idx + pos) * vocab_size +
                     vocab_offset,
                     mask=vocab_offset < vocab_size)
    temperature = tl.load(temperature_ptr + req_idx)
    if temperature == GREEDY_TEMPERATURE:
        # Greedy sampling. Just return the logits.
        output_prob = logits
    else:
        # Random sampling.
        output_prob = tl.softmax(logits / temperature)
    output_prob = output_prob.to(dtype=tl.float32)

    tl.store(output_prob_ptr + (start_idx + pos) * vocab_size + vocab_offset,
             output_prob,
             mask=vocab_offset < vocab_size)
