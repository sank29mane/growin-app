
import sys
import os
import shutil

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from rag_manager import RAGManager
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
    
    print("✅ RAGManager Test Passed")
    
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
