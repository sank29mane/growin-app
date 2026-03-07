
import sys
import os
import shutil

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from backend.rag_manager import RAGManager
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)

def test_rag():
    print("Testing RAGManager...")
    
    # Use a temp directory for test
    test_db_path = "./data/test_chroma_db"
    
    # Clean up previous test
    if os.path.exists(test_db_path):
        shutil.rmtree(test_db_path)
    
    rag = RAGManager(persistent_path=test_db_path)
    
    # 1. Test Adding Documents
    print("1. Adding documents...")
    rag.add_document(
        content="Apple reported Q3 earnings of $1.26 per share, beating expectations.",
        metadata={"type": "financial_report", "symbol": "AAPL", "date": "2023-11-02"},
        doc_id="doc1"
    )
    rag.add_document(
        content="Tesla delivery numbers fell short of analyst predictions due to supply chain issues.",
        metadata={"type": "news", "symbol": "TSLA", "date": "2023-10-18"},
        doc_id="doc2"
    )
    
    count = rag.count()
    print(f"   Count: {count}")
    assert count == 2
    
    # 2. Test Querying
    print("2. Querying 'Apple earnings'...")
    results = rag.query("How did Apple perform?", n_results=1)
    
    print(f"   Results: {results}")
    
    assert len(results) == 1
    assert "Apple" in results[0]['content']
    assert results[0]['metadata']['symbol'] == "AAPL"
    
    # 3. Test Chat History
    print("3. Testing chat history...")
    rag.add_chat_message(role="user", content="What is the stock price of AAPL?", conversation_id="conv123")
    rag.add_chat_message(role="assistant", content="AAPL is currently trading at $150.", conversation_id="conv123")
    
    chat_results = rag.query("AAPL price", where={"conversation_id": "conv123"})
    assert len(chat_results) > 0
    assert "150" in chat_results[0]['content']
    print("   Chat history retrieval passed")

    # 4. Test News Timeline
    print("4. Testing news timeline...")
    rag.add_news_article(
        ticker="TSLA", 
        title="Tesla Sales Surge", 
        summary="Tesla saw a 20% increase in sales this quarter.", 
        sentiment=0.8, 
        source="Reuters"
    )
    
    timeline = rag.get_news_timeline(ticker="TSLA", days=1)
    assert len(timeline) >= 1
    assert timeline[0]['sentiment'] == 0.8
    assert "Reuters" in timeline[0]['content']
    print("   News timeline passed")
    
    print("✅ RAGManager Comprehensive Test Passed")
    
    # Cleanup
    if os.path.exists(test_db_path):
        shutil.rmtree(test_db_path)

if __name__ == "__main__":
    try:
        test_rag()
    except Exception as e:
        print(f"❌ Test Failed: {e}")
        import traceback
        traceback.print_exc()
