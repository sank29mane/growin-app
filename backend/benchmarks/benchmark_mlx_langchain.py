import sys
import timeit
import tracemalloc
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

messages = [
    SystemMessage(content="You are a helpful assistant. "),
    HumanMessage(content="Hello "),
    AIMessage(content="Hi there! "),
    HumanMessage(content="What is the weather today? "),
    AIMessage(content="I don't have access to weather information. "),
    HumanMessage(content="Why not? "),
    AIMessage(content="Because I am an AI without internet access. "),
    HumanMessage(content="Oh, I see. What can you do? "),
    AIMessage(content="I can answer questions based on my training data. "),
    HumanMessage(content="Can you write a poem? "),
] * 1000

def old_method(messages):
    role_map = {
        SystemMessage: "system",
        HumanMessage: "user",
        AIMessage: "assistant",
    }
    prompt_parts = []
    for msg in messages:
        role = role_map.get(type(msg), "user")
        prompt_parts.append(f"<|im_start|>{role}\n{msg.content}<|im_end|>\n")

    prompt_parts.append("<|im_start|>assistant\n")
    return "".join(prompt_parts)

def new_method(messages):
    role_map = {
        SystemMessage: "system",
        HumanMessage: "user",
        AIMessage: "assistant",
    }
    return "".join(
        f"<|im_start|>{role_map.get(type(msg), 'user')}\n{msg.content}<|im_end|>\n"
        for msg in messages
    ) + "<|im_start|>assistant\n"

if __name__ == "__main__":
    t_old = timeit.timeit("old_method(messages)", globals=globals(), number=1000)
    t_new = timeit.timeit("new_method(messages)", globals=globals(), number=1000)

    tracemalloc.start()
    old_method(messages)
    _, peak_old = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    new_method(messages)
    _, peak_new = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"Old method CPU: {t_old:.4f}s")
    print(f"New method CPU: {t_new:.4f}s")
    print(f"CPU Improvement: {(t_old - t_new) / t_old * 100:.2f}%")
    print(f"Old method peak memory: {peak_old / 1024:.2f} KB")
    print(f"New method peak memory: {peak_new / 1024:.2f} KB")
    print(f"Memory reduction: {(peak_old - peak_new) / peak_old * 100:.2f}%")
