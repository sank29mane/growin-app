import asyncio
import threading

_sentiment_analyzer = None
_sentiment_lock = threading.Lock()

def _load_analyzer():
    global _sentiment_analyzer
    with _sentiment_lock:
        if _sentiment_analyzer is None:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            _sentiment_analyzer = SentimentIntensityAnalyzer()
    return _sentiment_analyzer

async def get_sentiment_analyzer():
    global _sentiment_analyzer
    if _sentiment_analyzer is None:
        return await asyncio.to_thread(_load_analyzer)
    return _sentiment_analyzer
