
import logging
import os
from typing import List, Dict, Any, Optional
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

class RAGManager:
    """
    Manages Retrieval-Augmented Generation (RAG) using ChromaDB.
    Stores and retrieves historical context, trade logs, and user preferences.
    """
    def __init__(self, persistent_path: str = "./data/chroma_db"):
        self.persistent_path = persistent_path
        self._client = None
        self._collection = None
        self._init_db()

    def _init_db(self):
        """Initialize ChromaDB client and collection"""
        try:
            # Ensure directory exists
            os.makedirs(self.persistent_path, exist_ok=True)
            
            self._client = chromadb.PersistentClient(path=self.persistent_path)
            
            # Use default embedding function (all-MiniLM-L6-v2)
            self._embedding_fn = embedding_functions.DefaultEmbeddingFunction()
            
            self._collection = self._client.get_or_create_collection(
                name="growin_knowledge_base",
                embedding_function=self._embedding_fn
            )
            logger.info(f"RAGManager initialized at {self.persistent_path}")
            
        except Exception as e:
            logger.error(f"Failed to initialize RAGManager: {e}")
            # Fallback to ephemeral client regarding failure vs crashing
            try:
                self._client = chromadb.Client()
                self._collection = self._client.get_or_create_collection("growin_temp")
                logger.warning("RAGManager using ephemeral in-memory DB due to initialization error.")
            except Exception as e2:
                logger.error(f"Critical RAG failure: {e2}")

            # Auto-seed abstract context if empty
            if self._collection and self._collection.count() == 0:
                self.seed_abstract_context()

    def seed_abstract_context(self):
        """Seed the database with broad macroeconomic and portfolio theory concepts."""
        abstract_docs = [
            {
                "content": "Sector Rotation Theory: During inflationary periods or rising interest rates, growth stocks (Tech, Consumer Discretionary) tend to underperform while value stocks (Financials, Energy, Materials) often outperform. A flat tech-heavy portfolio during rate hikes is expected.",
                "type": "abstract_concept",
                "topic": "sector_rotation"
            },
            {
                "content": "Portfolio Diversification and Correlation: If an entire portfolio is flat despite market movement, the assets might be highly correlated and cancelling each other out, or they are concentrated in a stagnant sector. High correlation reduces the benefit of diversification.",
                "type": "abstract_concept",
                "topic": "correlation"
            },
            {
                "content": "Macroeconomic Impact of Yield Curves: An inverted yield curve often signals an impending recession, leading investors to flee to safety (bonds, defensive stocks). Equity portfolios may stall or drop as liquidity tightens.",
                "type": "abstract_concept",
                "topic": "macroeconomics"
            }
        ]
        
        for doc in abstract_docs:
            self.add_document(
                content=doc["content"], 
                metadata={"type": doc["type"], "topic": doc["topic"]}
            )
        logger.info(f"RAGManager: Seeded {len(abstract_docs)} abstract financial concepts.")

    def add_document(self, content: str, metadata: Dict[str, Any], doc_id: Optional[str] = None):
        """
        Add a document to the knowledge base.
        
        Args:
            content: Text content to index
            metadata: Key-value pairs (e.g., {'type': 'trade_log', 'ticker': 'AAPL'})
            doc_id: Unique ID (auto-generated if None)
        """
        if not self._collection:
            logger.warning("RAG collection not available. Skipping add_document.")
            return

        try:
            import uuid
            if not doc_id:
                doc_id = str(uuid.uuid4())
            
            self._collection.add(
                documents=[content],
                metadatas=[metadata],
                ids=[doc_id]
            )
            logger.debug(f"Added document {doc_id} to RAG")
            
        except Exception as e:
            logger.error(f"Error adding document to RAG: {e}")

    def query(self, query_text: str, n_results: int = 3, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Retrieve relevant documents for a query.
        
        Returns:
            List of dicts with 'content' and 'metadata'
        """
        if not self._collection:
            return []

        try:
            kwargs = {
                "query_texts": [query_text],
                "n_results": n_results
            }
            if where:
                kwargs["where"] = where
                
            results = self._collection.query(**kwargs)
            
            # Format results
            formatted_results = []
            if results['documents']:
                for i, doc in enumerate(results['documents'][0]):
                    meta = results['metadatas'][0][i] if results['metadatas'] else {}
                    formatted_results.append({
                        "content": doc,
                        "metadata": meta,
                        "distance": results['distances'][0][i] if results['distances'] else 0
                    })
            
            return formatted_results
            
        except Exception as e:
            logger.error(f"RAG query failed: {e}")
            return []

    def add_chat_message(self, role: str, content: str, conversation_id: str):
        """Index a chat message for semantic retrieval."""
        from datetime import datetime, timezone
        metadata = {
            "type": "chat_history",
            "role": role,
            "conversation_id": conversation_id,
            "timestamp": datetime.now(timezone.utc).timestamp()
        }
        self.add_document(content, metadata)

    def add_news_article(self, ticker: str, title: str, summary: str, sentiment: float, source: str):
        """Index an individual news article with sentiment and timestamp."""
        from datetime import datetime, timezone
        metadata = {
            "type": "news_article",
            "ticker": ticker,
            "sentiment": sentiment,
            "source": source,
            "timestamp": datetime.now(timezone.utc).timestamp()
        }
        content = f"[{source}] {title}: {summary}"
        self.add_document(content, metadata)

    def get_news_timeline(self, ticker: str, days: int = 7) -> List[Dict[str, Any]]:
        """Retrieve a time-stamped timeline of news insights for a ticker."""
        from datetime import datetime, timedelta, timezone
        start_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        
        # Query for news articles related to the ticker
        # Note: ChromaDB doesn't support complex time-range filters in simple query,
        # we filter in-memory after a semantic search or use 'where' if supported.
        try:
            results = self._collection.get(
                where={"$and": [
                    {"type": "news_article"},
                    {"ticker": ticker},
                    {"timestamp": {"$gte": start_ts}}
                ]}
            )
            
            timeline = []
            if results['documents']:
                for i in range(len(results['documents'])):
                    timeline.append({
                        "timestamp": results['metadatas'][i]['timestamp'],
                        "content": results['documents'][i],
                        "sentiment": results['metadatas'][i]['sentiment'],
                        "source": results['metadatas'][i]['source']
                    })
            
            # Sort by timestamp ascending
            timeline.sort(key=lambda x: x['timestamp'])
            return timeline
        except Exception as e:
            logger.error(f"Failed to generate news timeline: {e}")
            return []

    def count(self) -> int:
        """Return number of documents in collection"""
        if self._collection:
            return self._collection.count()
        return 0
