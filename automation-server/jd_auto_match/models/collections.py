import uuid
import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from openai import OpenAI


class LMStudioEmbeddingFunction(EmbeddingFunction[Documents]):
    """兼容 openai>=1.0 的自定义 Embedding Function，用于连接 LM Studio"""

    def __init__(self, api_key: str, api_base: str, model_name: str):
        self._client = OpenAI(api_key=api_key, base_url=api_base)
        self._model_name = model_name

    def __call__(self, input: Documents) -> Embeddings:
        response = self._client.embeddings.create(input=input, model=self._model_name)
        return [item.embedding for item in response.data]


lm_studio_ef = LMStudioEmbeddingFunction(
    api_key="lm-studio",                 # LM Studio 不需要真实 key，填任意字符串即可
    api_base="http://localhost:1234/v1",  # 指向 LM Studio 的本地网关地址
    model_name="text-embedding-bge-large-zh-v1.5"    # 填入你在 LM Studio 中加载的模型名称
)
jdam_client = chromadb.PersistentClient(path="./jd_auto_match/models/storage")
job_collection = jdam_client.get_or_create_collection(name="job_postings", embedding_function=lm_studio_ef, metadata={"hnsw:space": "cosine"})

def add_job_semantic_only(title: str, company: str, summary: str, distance_threshold=0.2):
    """
    纯语义去重的职位插入函数
    :param distance_threshold: 拦截阈值，默认 0.1。值越小，去重越严格（要求极度相似才拦截）。
    """
    try:
        # 必须先判断库里有没有数据，空库直接 query 会报错
        if job_collection.count() > 0:
            # 在库中寻找语义最接近的 1 条记录
            search_results = job_collection.query(
                query_texts=[summary],
                n_results=1
            )
            
            if search_results and search_results['distances'] and len(search_results['distances'][0]) > 0:
                closest_distance = search_results['distances'][0][0]
                closest_meta = search_results['metadatas'][0][0]
                
                # 【核心拦截逻辑】
                if closest_distance < distance_threshold:
                    print(f"⚠️ [语义拦截] 拒绝入库！检测到高度相似的职位。")
                    print(f"   |> 传入职位信息: {company} - {title}')")
                    print(f"   |> 库中相似职位: {closest_meta.get('company')} - {closest_meta.get('title')}")
                    print(f"   |> 当前语义距离: {closest_distance:.4f} (拦截阈值: {distance_threshold})")
                    return True
                else:
                    print(f"🔍 [检查通过] 最相似记录距离为 {closest_distance:.4f}，大于阈值，判定为新职位。")
                    
    except Exception as e:
        print(f"语义检索阶段发生异常: {e}")

    # 顺利通过检查（或者库本身是空的），分配随机 UUID 并入库
    new_id = str(uuid.uuid4())
    
    job_collection.add(
        ids=[new_id],
        documents=[summary],
        metadatas=[{
            "title": title,
            "company": company
        }]
    )
    print(f"✅ [成功入库] {company} - {title} (分配ID: {new_id})")
    return False
