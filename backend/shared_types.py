from pydantic import BaseModel
from typing import List, Optional, Dict

class ModelCapabilities(BaseModel):
    quantization: str
    sizeCategory: str
    isInstruct: bool
    recommendedRAM: str

class HFModel(BaseModel):
    id: str
    name: str
    downloads: int
    likes: int
    author: str
    tags: List[str]
    pipeline_tag: Optional[str] = None
    sizeOnDisk: float
    modelSize: str
    downloadTime: str
    capabilities: Optional[ModelCapabilities] = None
    isCompatible: bool = True

class MLXModel(BaseModel):
    repoId: str
    displayName: str
    description: str
    sizeGb: float
    quantization: str
    downloads: int
    likes: int
    isCached: bool
    combinedScore: float = 0.0

class ModelProvider(BaseModel):
    name: str
    models: List[str]
    description: Optional[str] = None
    endpoint: Optional[str] = None
    requires_api_key: Optional[bool] = False

class MCPServer(BaseModel):
    name: str
    type: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    active: bool = False

class AvailableModelsResponse(BaseModel):
    providers: List[ModelProvider]
    default: Dict[str, str]

class MLXModelsResponse(BaseModel):
    models: List[MLXModel]
