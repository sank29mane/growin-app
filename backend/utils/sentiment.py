import asyncio
import threading

_sentiment_analyzer = None
_sentiment_lock = threading.Lock()

def get_sentiment_analyzer():
    """
    Synchronous fallback for backwards compatibility or sync contexts.
    Will block if not already loaded.
    """
    global _sentiment_analyzer
    with _sentiment_lock:
        if _sentiment_analyzer is None:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            _sentiment_analyzer = SentimentIntensityAnalyzer()
    return _sentiment_analyzer

async def get_sentiment_analyzer_async():
    """
    Asynchronously loads the SentimentIntensityAnalyzer in a background thread
    if it's not already loaded, preventing event loop blocking.
    """
    global _sentiment_analyzer
    if _sentiment_analyzer is not None:
        return _sentiment_analyzer

    return await asyncio.to_thread(get_sentiment_analyzer)
