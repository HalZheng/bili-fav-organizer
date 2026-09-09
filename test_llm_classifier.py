"""LLM分类器单元测试"""

import pytest
from unittest.mock import AsyncMock, patch

from config import BiliConfig
from models import BiliVideo, BiliUP
from classifier import LLMClassifier


@pytest.fixture
def config():
    cfg = BiliConfig()
    cfg.LLM_API_URL = "https://api.deepseek.com/v1/chat/completions"
    cfg.LLM_API_KEY = "test-api-key"
    cfg.LLM_MODEL = "deepseek-v4-flash"
    cfg.BUCKET_NAMES = ["tmp_游戏", "tmp_科技", "tmp_影视", "tmp_生活", "tmp_待分类"]
    cfg.LLM_BATCH_SIZE = 5
    cfg.LLM_CONFIDENCE_THRESHOLD = "medium"
    cfg.DEFAULT_BUCKET = "tmp_待分类"
    return cfg


@pytest.fixture
def sample_videos():
    return [
        BiliVideo(bvid="BV123", title="原神新版本攻略", upper=BiliUP(name="游戏主播")),
        BiliVideo(bvid="BV456", title="iPhone 16开箱评测", upper=BiliUP(name="科技达人")),
        BiliVideo(bvid="BV789", title="流浪地球3预告解析", upper=BiliUP(name="影评人")),
        BiliVideo(bvid="BV012", title="周末vlog日常", upper=BiliUP(name="生活博主")),
        BiliVideo(bvid="BV345", title="Python入门教程", upper=BiliUP(name="编程老师")),
    ]


def test_build_prompt(config, sample_videos):
    """测试Prompt构建"""
    classifier = LLMClassifier(config)
    video_dicts = [v.to_dict() for v in sample_videos]
    
    prompt = classifier._build_prompt(video_dicts, config.BUCKET_NAMES)
    
    assert "请对以下B站视频进行分类" in prompt
    for video in sample_videos:
        assert video.title in prompt
        assert video.upper.name in prompt
    for bucket in config.BUCKET_NAMES:
        assert bucket in prompt
    assert '"1": {"bucket": "类别名", "confidence": "high/medium/low"}' in prompt


@pytest.mark.asyncio
async def test_classify_batch_success(config, sample_videos):
    """测试批量分类成功"""
    classifier = LLMClassifier(config)
    
    mock_response_content = '{"1": {"bucket": "tmp_游戏", "confidence": "high"}, "2": {"bucket": "tmp_科技", "confidence": "high"}, "3": {"bucket": "tmp_影视", "confidence": "high"}, "4": {"bucket": "tmp_生活", "confidence": "high"}, "5": {"bucket": "tmp_科技", "confidence": "medium"}}'
    
    mock_client = AsyncMock()
    mock_client.post.return_value.__aenter__.return_value.json.return_value = {
        "choices": [{"message": {"content": mock_response_content}}]
    }
    mock_client.post.return_value.__aenter__.return_value.raise_for_status = lambda: None
    
    with patch.object(classifier, '_make_api_call', new_callable=AsyncMock) as mock_api_call:
        mock_api_call.return_value = mock_response_content
        
        result = await classifier.classify_batch(sample_videos, batch_size=5)
        
        assert len(result) == 5
        assert result["BV123"] == "tmp_游戏"
        assert result["BV456"] == "tmp_科技"
        assert result["BV789"] == "tmp_影视"
        assert result["BV345"] == "tmp_待分类"


@pytest.mark.asyncio
async def test_classify_batch_confidence_filter(config, sample_videos):
    """测试置信度过滤（medium/low归入待分类）"""
    classifier = LLMClassifier(config)
    
    mock_response_content = '{"1": {"bucket": "tmp_游戏", "confidence": "high"}, "2": {"bucket": "tmp_科技", "confidence": "medium"}, "3": {"bucket": "tmp_影视", "confidence": "low"}, "4": {"bucket": "tmp_生活", "confidence": "high"}, "5": {"bucket": "tmp_科技", "confidence": "high"}}'
    
    with patch.object(classifier, '_make_api_call', new_callable=AsyncMock) as mock_api_call:
        mock_api_call.return_value = mock_response_content
        
        result = await classifier.classify_batch(sample_videos, batch_size=5)
        
        assert result["BV123"] == "tmp_游戏"
        assert result["BV456"] == "tmp_待分类"
        assert result["BV789"] == "tmp_待分类"
        assert result["BV012"] == "tmp_生活"


@pytest.mark.asyncio
async def test_classify_batch_invalid_bucket(config, sample_videos):
    """测试非法桶名称自动归入待分类"""
    classifier = LLMClassifier(config)
    
    mock_response_content = '{"1": {"bucket": "不存在的类别", "confidence": "high"}, "2": {"bucket": "tmp_科技", "confidence": "high"}}'
    
    with patch.object(classifier, '_make_api_call', new_callable=AsyncMock) as mock_api_call:
        mock_api_call.return_value = mock_response_content
        
        result = await classifier.classify_batch(sample_videos[:2], batch_size=5)
        
        assert result["BV123"] == "tmp_待分类"
        assert result["BV456"] == "tmp_科技"


@pytest.mark.asyncio
async def test_classify_batch_api_not_configured():
    """测试API未配置时返回空结果"""
    cfg = BiliConfig()
    cfg.LLM_API_URL = ""
    cfg.LLM_API_KEY = ""
    cfg.BUCKET_NAMES = ["tmp_游戏", "tmp_科技"]
    cfg.DEFAULT_BUCKET = "tmp_待分类"
    
    classifier = LLMClassifier(cfg)
    sample = [BiliVideo(bvid="BV123", title="测试视频", upper=BiliUP(name="测试UP"))]
    
    result = await classifier.classify_batch(sample)
    
    assert result == {}


@pytest.mark.asyncio
async def test_classify_batch_api_error(config, sample_videos):
    """测试API调用失败时的异常处理"""
    classifier = LLMClassifier(config)
    
    with patch.object(classifier, '_make_api_call', new_callable=AsyncMock) as mock_api_call:
        mock_api_call.side_effect = Exception("网络超时")
        
        result = await classifier.classify_batch(sample_videos[:2], batch_size=5)
        
        assert result == {}


@pytest.mark.asyncio
async def test_classify_batch_markdown_codeblock(config, sample_videos):
    """测试LLM返回带markdown代码块的响应"""
    classifier = LLMClassifier(config)
    
    mock_response_content = '```json\n{"1": {"bucket": "tmp_游戏", "confidence": "high"}}\n```'
    
    with patch.object(classifier, '_make_api_call', new_callable=AsyncMock) as mock_api_call:
        mock_api_call.return_value = mock_response_content
        
        result = await classifier.classify_batch(sample_videos[:1], batch_size=5)
        
        assert result["BV123"] == "tmp_游戏"


@pytest.mark.asyncio
async def test_classify_batch_compat_old_format(config, sample_videos):
    """测试兼容旧格式（直接返回桶名称字符串）"""
    classifier = LLMClassifier(config)
    
    mock_response_content = '{"1": "tmp_游戏", "2": "tmp_科技"}'
    
    with patch.object(classifier, '_make_api_call', new_callable=AsyncMock) as mock_api_call:
        mock_api_call.return_value = mock_response_content
        
        result = await classifier.classify_batch(sample_videos[:2], batch_size=5)
        
        assert result["BV123"] == "tmp_游戏"
        assert result["BV456"] == "tmp_科技"


@pytest.mark.asyncio
async def test_classify_batch_mocked(config, sample_videos):
    """测试批量分类（mock底层方法）"""
    classifier = LLMClassifier(config)
    
    mock_result = {
        "BV123": "tmp_游戏",
        "BV456": "tmp_科技",
        "BV789": "tmp_待分类",
        "BV012": "tmp_生活",
        "BV345": "tmp_待分类",
    }
    
    with patch.object(classifier, 'classify_batch', new_callable=AsyncMock) as mock_classify_batch:
        mock_classify_batch.return_value = mock_result
        
        result = await classifier.classify_batch(sample_videos)
        
        assert result["BV123"] == "tmp_游戏"
        assert result["BV456"] == "tmp_科技"


def test_classify_all_sync_empty_videos():
    """测试空视频列表"""
    cfg = BiliConfig()
    cfg.LLM_API_URL = ""
    cfg.LLM_API_KEY = ""
    cfg.BUCKET_NAMES = ["tmp_游戏", "tmp_科技"]
    cfg.DEFAULT_BUCKET = "tmp_待分类"
    
    classifier = LLMClassifier(cfg)
    
    classified, unclassified = classifier.classify_all_sync([])
    
    assert classified == {}
    assert unclassified == []


def test_llm_config_defaults():
    """测试LLM配置默认值"""
    cfg = BiliConfig()
    
    assert cfg.LLM_API_URL == "https://api.deepseek.com/v1/chat/completions"
    assert cfg.LLM_MODEL == "deepseek-v4-flash"
    assert cfg.LLM_BATCH_SIZE == 20
    assert cfg.LLM_CONFIDENCE_THRESHOLD == "medium"