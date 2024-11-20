# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

import json
import warnings
from typing import Any, AsyncGenerator, Dict, Generator, List, Optional

from llama_models.llama3.api.datatypes import (
    CompletionMessage,
    StopReason,
    TokenLogProbs,
    ToolCall,
)
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk as OpenAIChatCompletionChunk
from openai.types.chat.chat_completion import (
    Choice as OpenAIChoice,
    ChoiceLogprobs as OpenAIChoiceLogprobs,  # same as chat_completion_chunk ChoiceLogprobs
)
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall as OpenAIChatCompletionMessageToolCall,
)

from llama_stack.apis.inference import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseEvent,
    ChatCompletionResponseEventType,
    ChatCompletionResponseStreamChunk,
    Message,
    ToolCallDelta,
    ToolCallParseStatus,
)


def _convert_message(message: Message) -> Dict:
    """
    Convert a Message to an OpenAI API-compatible dictionary.
    """
    out_dict = message.dict()
    # Llama Stack uses role="ipython" for tool call messages, OpenAI uses "tool"
    if out_dict["role"] == "ipython":
        out_dict.update(role="tool")

    if "stop_reason" in out_dict:
        out_dict.update(stop_reason=out_dict["stop_reason"].value)

    # TODO(mf): tool_calls

    return out_dict


def convert_chat_completion_request(
    request: ChatCompletionRequest,
    n: int = 1,
) -> dict:
    """
    Convert a ChatCompletionRequest to an OpenAI API-compatible dictionary.
    """
    # model -> model
    # messages -> messages
    # sampling_params  TODO(mattf): review strategy
    #  strategy=greedy -> nvext.top_k = -1, temperature = temperature
    #  strategy=top_p -> nvext.top_k = -1, top_p = top_p
    #  strategy=top_k -> nvext.top_k = top_k
    #  temperature -> temperature
    #  top_p -> top_p
    #  top_k -> nvext.top_k
    #  max_tokens -> max_tokens
    #  repetition_penalty -> nvext.repetition_penalty
    # tools -> tools
    # tool_choice ("auto", "required") -> tool_choice
    # tool_prompt_format -> TBD
    # stream -> stream
    # logprobs -> logprobs

    nvext = {}
    payload: Dict[str, Any] = dict(
        model=request.model,
        messages=[_convert_message(message) for message in request.messages],
        stream=request.stream,
        n=n,
        extra_body=dict(nvext=nvext),
        extra_headers={
            b"User-Agent": b"llama-stack: nvidia-inference-adapter",
        },
    )

    if request.tools:
        payload.update(tools=request.tools)
        if request.tool_choice:
            payload.update(
                tool_choice=request.tool_choice.value
            )  # we cannot include tool_choice w/o tools, server will complain

    if request.logprobs:
        payload.update(logprobs=True)
        payload.update(top_logprobs=request.logprobs.top_k)

    if request.sampling_params:
        nvext.update(repetition_penalty=request.sampling_params.repetition_penalty)

        if request.sampling_params.max_tokens:
            payload.update(max_tokens=request.sampling_params.max_tokens)

        if request.sampling_params.strategy == "top_p":
            nvext.update(top_k=-1)
            payload.update(top_p=request.sampling_params.top_p)
        elif request.sampling_params.strategy == "top_k":
            if (
                request.sampling_params.top_k != -1
                and request.sampling_params.top_k < 1
            ):
                warnings.warn("top_k must be -1 or >= 1")
            nvext.update(top_k=request.sampling_params.top_k)
        elif request.sampling_params.strategy == "greedy":
            nvext.update(top_k=-1)
            payload.update(temperature=request.sampling_params.temperature)

    return payload


def _convert_openai_finish_reason(finish_reason: str) -> StopReason:
    """
    Convert an OpenAI chat completion finish_reason to a StopReason.

    finish_reason: Literal["stop", "length", "tool_calls", ...]
        - stop: model hit a natural stop point or a provided stop sequence
        - length: maximum number of tokens specified in the request was reached
        - tool_calls: model called a tool

    ->

    class StopReason(Enum):
        end_of_turn = "end_of_turn"
        end_of_message = "end_of_message"
        out_of_tokens = "out_of_tokens"
    """

    # TODO(mf): are end_of_turn and end_of_message semantics correct?
    return {
        "stop": StopReason.end_of_turn,
        "length": StopReason.out_of_tokens,
        "tool_calls": StopReason.end_of_message,
    }.get(finish_reason, StopReason.end_of_turn)


def _convert_openai_tool_calls(
    tool_calls: List[OpenAIChatCompletionMessageToolCall],
) -> List[ToolCall]:
    """
    Convert an OpenAI ChatCompletionMessageToolCall list into a list of ToolCall.

    OpenAI ChatCompletionMessageToolCall:
        id: str
        function: Function
        type: Literal["function"]

    OpenAI Function:
        arguments: str
        name: str

    ->

    ToolCall:
        call_id: str
        tool_name: str
        arguments: Dict[str, ...]
    """
    if not tool_calls:
        return []  # CompletionMessage tool_calls is not optional

    return [
        ToolCall(
            call_id=call.id,
            tool_name=call.function.name,
            arguments=json.loads(call.function.arguments),
        )
        for call in tool_calls
    ]


def _convert_openai_logprobs(
    logprobs: OpenAIChoiceLogprobs,
) -> Optional[List[TokenLogProbs]]:
    """
    Convert an OpenAI ChoiceLogprobs into a list of TokenLogProbs.

    OpenAI ChoiceLogprobs:
        content: Optional[List[ChatCompletionTokenLogprob]]

    OpenAI ChatCompletionTokenLogprob:
        token: str
        logprob: float
        top_logprobs: List[TopLogprob]

    OpenAI TopLogprob:
        token: str
        logprob: float

    ->

    TokenLogProbs:
        logprobs_by_token: Dict[str, float]
         - token, logprob

    """
    if not logprobs:
        return None

    return [
        TokenLogProbs(
            logprobs_by_token={
                logprobs.token: logprobs.logprob for logprobs in content.top_logprobs
            }
        )
        for content in logprobs.content
    ]


def convert_openai_chat_completion_choice(
    choice: OpenAIChoice,
) -> ChatCompletionResponse:
    """
    Convert an OpenAI Choice into a ChatCompletionResponse.

    OpenAI Choice:
        message: ChatCompletionMessage
        finish_reason: str
        logprobs: Optional[ChoiceLogprobs]

    OpenAI ChatCompletionMessage:
        role: Literal["assistant"]
        content: Optional[str]
        tool_calls: Optional[List[ChatCompletionMessageToolCall]]

    ->

    ChatCompletionResponse:
        completion_message: CompletionMessage
        logprobs: Optional[List[TokenLogProbs]]

    CompletionMessage:
        role: Literal["assistant"]
        content: str | ImageMedia | List[str | ImageMedia]
        stop_reason: StopReason
        tool_calls: List[ToolCall]

    class StopReason(Enum):
        end_of_turn = "end_of_turn"
        end_of_message = "end_of_message"
        out_of_tokens = "out_of_tokens"
    """
    assert (
        hasattr(choice, "message") and choice.message
    ), "error in server response: message not found"
    assert (
        hasattr(choice, "finish_reason") and choice.finish_reason
    ), "error in server response: finish_reason not found"

    return ChatCompletionResponse(
        completion_message=CompletionMessage(
            content=choice.message.content
            or "",  # CompletionMessage content is not optional
            stop_reason=_convert_openai_finish_reason(choice.finish_reason),
            tool_calls=_convert_openai_tool_calls(choice.message.tool_calls),
        ),
        logprobs=_convert_openai_logprobs(choice.logprobs),
    )


async def convert_openai_chat_completion_stream(
    stream: AsyncStream[OpenAIChatCompletionChunk],
) -> AsyncGenerator[ChatCompletionResponseStreamChunk, None]:
    """
    Convert a stream of OpenAI chat completion chunks into a stream
    of ChatCompletionResponseStreamChunk.

    OpenAI ChatCompletionChunk:
        choices: List[Choice]

    OpenAI Choice:  # different from the non-streamed Choice
        delta: ChoiceDelta
        finish_reason: Optional[Literal["stop", "length", "tool_calls", "content_filter", "function_call"]]
        logprobs: Optional[ChoiceLogprobs]

    OpenAI ChoiceDelta:
        content: Optional[str]
        role: Optional[Literal["system", "user", "assistant", "tool"]]
        tool_calls: Optional[List[ChoiceDeltaToolCall]]

    OpenAI ChoiceDeltaToolCall:
        index: int
        id: Optional[str]
        function: Optional[ChoiceDeltaToolCallFunction]
        type: Optional[Literal["function"]]

    OpenAI ChoiceDeltaToolCallFunction:
        name: Optional[str]
        arguments: Optional[str]

    ->

    ChatCompletionResponseStreamChunk:
        event: ChatCompletionResponseEvent

    ChatCompletionResponseEvent:
        event_type: ChatCompletionResponseEventType
        delta: Union[str, ToolCallDelta]
        logprobs: Optional[List[TokenLogProbs]]
        stop_reason: Optional[StopReason]

    ChatCompletionResponseEventType:
        start = "start"
        progress = "progress"
        complete = "complete"

    ToolCallDelta:
        content: Union[str, ToolCall]
        parse_status: ToolCallParseStatus

    ToolCall:
        call_id: str
        tool_name: str
        arguments: str

    ToolCallParseStatus:
        started = "started"
        in_progress = "in_progress"
        failure = "failure"
        success = "success"

    TokenLogProbs:
        logprobs_by_token: Dict[str, float]
         - token, logprob

    StopReason:
        end_of_turn = "end_of_turn"
        end_of_message = "end_of_message"
        out_of_tokens = "out_of_tokens"
    """

    # generate a stream of ChatCompletionResponseEventType: start -> progress -> progress -> ...
    def _event_type_generator() -> (
        Generator[ChatCompletionResponseEventType, None, None]
    ):
        yield ChatCompletionResponseEventType.start
        while True:
            yield ChatCompletionResponseEventType.progress

    event_type = _event_type_generator()

    # we implement NIM specific semantics, the main difference from OpenAI
    # is that tool_calls are always produced as a complete call. there is no
    # intermediate / partial tool call streamed. because of this, we can
    # simplify the logic and not concern outselves with parse_status of
    # started/in_progress/failed. we can always assume success.
    #
    # a stream of ChatCompletionResponseStreamChunk consists of
    #  0. a start event
    #  1. zero or more progress events
    #   - each progress event has a delta
    #   - each progress event may have a stop_reason
    #   - each progress event may have logprobs
    #   - each progress event may have tool_calls
    #     if a progress event has tool_calls,
    #      it is fully formed and
    #      can be emitted with a parse_status of success
    #  2. a complete event

    stop_reason = None

    async for chunk in stream:
        choice = chunk.choices[0]  # assuming only one choice per chunk

        # we assume there's only one finish_reason in the stream
        stop_reason = _convert_openai_finish_reason(choice.finish_reason) or stop_reason

        # if there's a tool call, emit an event for each tool in the list
        # if tool call and content, emit both separately

        if choice.delta.tool_calls:
            # the call may have content and a tool call. ChatCompletionResponseEvent
            # does not support both, so we emit the content first
            if choice.delta.content:
                yield ChatCompletionResponseStreamChunk(
                    event=ChatCompletionResponseEvent(
                        event_type=next(event_type),
                        delta=choice.delta.content,
                        logprobs=_convert_openai_logprobs(choice.logprobs),
                    )
                )

            # it is possible to have parallel tool calls in stream, but
            # ChatCompletionResponseEvent only supports one per stream
            if len(choice.delta.tool_calls) > 1:
                warnings.warn(
                    "multiple tool calls found in a single delta, using the first, ignoring the rest"
                )

            # NIM only produces fully formed tool calls, so we can assume success
            yield ChatCompletionResponseStreamChunk(
                event=ChatCompletionResponseEvent(
                    event_type=next(event_type),
                    delta=ToolCallDelta(
                        content=_convert_openai_tool_calls(choice.delta.tool_calls)[0],
                        parse_status=ToolCallParseStatus.success,
                    ),
                    logprobs=_convert_openai_logprobs(choice.logprobs),
                )
            )
        else:
            yield ChatCompletionResponseStreamChunk(
                event=ChatCompletionResponseEvent(
                    event_type=next(event_type),
                    delta=choice.delta.content or "",  # content is not optional
                    logprobs=_convert_openai_logprobs(choice.logprobs),
                )
            )

    yield ChatCompletionResponseStreamChunk(
        event=ChatCompletionResponseEvent(
            event_type=ChatCompletionResponseEventType.complete,
            delta="",
            stop_reason=stop_reason,
        )
    )