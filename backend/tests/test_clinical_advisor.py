from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.providers.clinical_advisor import QwenClinicalAdvisor, build_clinical_advisor
from app.providers.qwen_client import DraftProviderError, QwenJsonClient


CONTEXT = {"transcript_revision": 1, "transcript_complete": False, "segments": [
    {"id": "s1", "seq": 0, "speaker": "未知", "text": "医生：有发热吗？患者：咳嗽三天，没有发热。"},
]}


def suggestion(**changes):
    return {"category": "diagnostic_consideration", "title": "咳嗽病因待鉴别",
            "suggestion": "可结合完整病史与查体评估感染等可能病因。",
            "rationale": "原文提到咳嗽且否认发热，仍不足以确诊。",
            "source_segment_ids": ["s1"], "missing_information": ["年龄、基础病及查体结果"],
            "topic_key": None, "existing_item_id": None, "history_field_keys": [], **changes}


def provider_response(suggestions):
    return {"id": "request-test", "model": "qwen3.7-plus", "usage": {
        "prompt_tokens": 100, "completion_tokens": 50,
    }, "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"suggestions": suggestions}, ensure_ascii=False),
    }}]}


def test_qwen_advisor_uses_independent_model_and_json_contract(settings):
    settings.qwen_base_url = "https://test.example/v1"
    settings.qwen_api_key = SecretStr("test-secret")
    settings.qwen_model = "record-model"
    settings.assistant_model = "qwen3.7-plus"
    provider = build_clinical_advisor(settings)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == "https://test.example/v1/chat/completions"
        return httpx.Response(200, json=provider_response([
            suggestion(), suggestion(category="examination"), suggestion(category="treatment"),
        ]))

    provider.client.transport = httpx.MockTransport(respond)
    result = provider.suggest(CONTEXT)
    body = calls[0]
    assert body["model"] == "qwen3.7-plus" and settings.qwen_model == "record-model"
    assert body["enable_thinking"] is False and body["temperature"] == 0
    assert body["response_format"]["json_schema"]["strict"]
    assert body["messages"][0]["role"] == "system"
    assert json.loads(body["messages"][1]["content"]) == CONTEXT
    assert len(result.suggestions) == 3
    assert result.metadata["prompt_version"] == "clinical-advisor-v2"
    assert result.metadata["usage"]["prompt_tokens"] == 100


@pytest.mark.parametrize("changes", [
    {"source_segment_ids": ["other-session-id"]}, {"source_segment_ids": []},
    {"category": "definitive_diagnosis"}, {"suggestion": " "}, {"unexpected": "extra"},
    {"history_field_keys": ["allergy_history"]}, {"existing_item_id": "other-patient-task"},
])
def test_advisor_rejects_invalid_evidence_and_schema_and_retains_usage(changes):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=provider_response([suggestion(**changes)]))

    provider = QwenClinicalAdvisor(QwenJsonClient("https://test.example/v1", "qwen3.7-plus", "test-secret",
                                                transport=httpx.MockTransport(respond)))
    with pytest.raises(DraftProviderError) as error:
        provider.suggest(CONTEXT)
    assert error.value.code == "advice_invalid_response"
    assert error.value.metadata["usage"]["prompt_tokens"] == 100
    assert len(calls) == 1


def test_empty_advice_and_duplicate_evidence_are_valid():
    responses = iter([[], [suggestion(source_segment_ids=["s1", "s1"]), suggestion()]])
    provider = QwenClinicalAdvisor(QwenJsonClient("https://test.example/v1", "qwen3.7-plus", "test-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=provider_response(next(responses))))))
    assert provider.suggest(CONTEXT).suggestions == ()
    result = provider.suggest(CONTEXT)
    assert len(result.suggestions) == 1 and result.suggestions[0]["source_segment_ids"] == ["s1"]
