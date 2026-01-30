import chromadb
from chromadb.config import Settings
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class RAGManager:
    """
    Local RAG (Retrieval-Augmented Generation) Manager using ChromaDB.
    Persists embeddings of chat history and important context.
    """
    def __init__(self, persist_directory: str = "growin_rag_db"):
        try:
            self.client = chromadb.PersistentClient(path=persist_directory)
            self.collection = self.client.get_or_create_collection(name="growin_knowledge")
            logger.info(f"✅ RAG Manager initialized at {persist_directory}")
        except Exception as e:
            logger.error(f"❌ RAG Manager initialization failed: {e}")
            self.client = None
            self.collection = None

    def add_document(self, doc_id: str, text: str, metadata: Dict[str, Any] = None):
        """Add or update a document in the vector store"""
        if not self.collection: return
        
        try:
            self.collection.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata or {}]
            )
        except Exception as e:
            logger.error(f"Failed to add document using RAG: {e}")

    def query(self, query_text: str, n_results: int = 3, where: Optional[Dict] = None) -> List[Dict]:
        """Semantic search for relevant documents"""
        if not self.collection: return []
        
        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=n_results,
                where=where
            )
            
            output = []
            if results and results['documents']:
                # Chroma returns list of lists (one per query)
                docs = results['documents'][0]
                metas = results['metadatas'][0] if results['metadatas'] else [{}] * len(docs)
                dists = results['distances'][0] if results['distances'] else [0.0] * len(docs)
                
                for i, doc in enumerate(docs):
                    output.append({
                        "content": doc,
                        "metadata": metas[i],
                        "distance": dists[i]
                    })
            return output
        except Exception as e:
            logger.error(f"RAG query failed: {e}")
            return []
            
    def index_chat_message(self, message_id: str, role: str, content: str, timestamp: str):
        """Helper to index a chat message"""
        text = f"{role.upper()}: {content}"
        meta = {"type": "chat_log", "role": role, "timestamp": str(timestamp)}
        self.add_document(message_id, text, meta)
