
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

    def query(self, query_text: str, n_results: int = 3) -> List[Dict[str, Any]]:
        """
        Retrieve relevant documents for a query.
        
        Returns:
            List of dicts with 'content' and 'metadata'
        """
        if not self._collection:
            return []

        try:
            results = self._collection.query(
                query_texts=[query_text],
                n_results=n_results
            )
            
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

    def count(self) -> int:
        """Return number of documents in collection"""
        if self._collection:
            return self._collection.count()
        return 0
