import time
import threading
import json
from modelscope import snapshot_download
from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer


# 定义一个简单的示例工具
def get_weather(city: str) -> str:
    """获取指定城市的天气信息"""
    return f"{city} 今天的天气是晴朗，气温 25°C。"


def get_stock_price(symbol: str) -> str:
    """获取指定股票代码的价格"""
    return f"{symbol} 的当前股价是 $150.00。"


# 注册可用工具列表
AVAILABLE_TOOLS = {
    "get_weather": get_weather,
    "get_stock_price": get_stock_price
}

# 定义工具的 JSON Schema (符合 OpenAI/ChatML 标准)
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市的天气信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称，如北京、上海"}
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "获取指定股票的实时价格",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 AAPL, TSLA"}
                },
                "required": ["symbol"]
            }
        }
    }
]


class QwenChatbot:
    def __init__(self, model_name="Qwen/Qwen3-0.6B", sys_prompt: str = ""):
        print("正在加载模型...")
        # 下载模型
        model_dir = snapshot_download(model_name, cache_dir="./models")

        # 加载分词器和模型
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_dir,
            torch_dtype="auto",
            device_map="auto"
        )
        self.history = []
        if sys_prompt:
            self.history.append({"role": "system", "content": sys_prompt})

    def _execute_tool(self, tool_calls):
        """
        解析模型生成的工具调用请求，并执行本地函数
        """
        results = []
        for tool_call in tool_calls:
            func_name = tool_call['function']['name']
            arguments = json.loads(tool_call['function']['arguments'])

            if func_name in AVAILABLE_TOOLS:
                try:
                    # 执行函数
                    result = AVAILABLE_TOOLS[func_name](**arguments)
                    results.append({
                        "role": "tool",
                        "content": str(result),
                        "name": func_name
                    })
                    print(f"\n[工具执行]: {func_name} -> {result}")
                except Exception as e:
                    results.append({
                        "role": "tool",
                        "content": f"Error: {str(e)}",
                        "name": func_name
                    })
            else:
                results.append({
                    "role": "tool",
                    "content": f"Error: Function {func_name} not found",
                    "name": func_name
                })
        return results

    def generate_response_stream(self, user_input, tools=None):
        """
        流式生成回复，支持工具调用和 TTFT 统计
        :param user_input: 用户输入
        :param tools: 可选的工具列表 schema，如果提供则启用工具调用
        """
        messages = self.history + [{"role": "user", "content": user_input}]

        # 1. 应用聊天模板
        # 注意：如果传入了 tools，需要将其传递给 apply_chat_template
        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            tools=tools,  # 传入工具定义
            enable_thinking=False
        )

        # 2. 初始化 TextIteratorStreamer
        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True)

        # 3. 准备模型输入
        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)

        # 4. 定义生成_kwargs
        generation_kwargs = dict(
            inputs,
            streamer=streamer,
            max_new_tokens=32768,
            do_sample=True,
            temperature=0.7,
            top_p=0.9
        )

        # 5. 在后台线程中运行模型生成
        thread = threading.Thread(target=self.model.generate, kwargs=generation_kwargs)
        thread.start()

        # 6. 主线程迭代 streamer 获取实时输出并统计时间
        generated_text = ""
        first_token_time = None
        start_time = time.perf_counter()

        print("Bot: ", end="", flush=True)

        for new_text in streamer:
            # 计算 TTFT (Time To First Token)
            if first_token_time is None:
                first_token_time = time.perf_counter()
                ttft = first_token_time - start_time
                print(f"\n[TTFT: {ttft:.4f}s] ", end="", flush=True)  # 可选：显示首字延迟

            print(new_text, end="", flush=True)
            generated_text += new_text

        print()  # 换行
        thread.join()  # 等待生成线程完全结束

        total_time = time.perf_counter() - start_time
        print(f"[总耗时: {total_time:.2f}s, TPS: {len(self.tokenizer.encode(generated_text)) / total_time:.1f}]")

        # 7. 处理工具调用逻辑
        # Qwen3 通常会在输出中包含 <tool_call> 标签或特定的 JSON 结构
        # 这里我们需要检查生成的文本是否包含工具调用指令
        # 注意：具体的解析逻辑取决于 Qwen3 的 template 实现，通常建议让模型输出完整后再解析，
        # 或者在流式中检测特定结束符。为简化演示，此处假设如果使用了 tools，
        # 我们需要再次检查 generated_text 是否触发了工具。

        final_messages = messages.copy()
        final_messages.append({"role": "assistant", "content": generated_text})

        # 尝试解析工具调用 (简单启发式：检查是否包含 tool_call 标记或 JSON)
        # 实际生产中建议使用 model.chat 接口或更完善的 parser
        tool_calls = self._parse_tool_calls_from_text(generated_text)

        if tool_calls:
            # 如果有工具调用，执行工具
            tool_results = self._execute_tool(tool_calls)

            # 将工具结果加入历史，并进行第二轮对话（生成最终自然语言回答）
            final_messages.extend(tool_results)

            # 递归或再次调用生成（此时不再传 tools，或者继续传以支持多步工具调用）
            # 为了保持流式体验，这里简化为再次调用流式生成
            print("\n[正在根据工具结果生成最终回答...]")
            return self._generate_final_answer_stream(final_messages, tools=None)

        else:
            # 无工具调用，直接更新历史
            self.history.append({"role": "user", "content": user_input})
            self.history.append({"role": "assistant", "content": generated_text})
            return generated_text

    def _parse_tool_calls_from_text(self, text):
        """
        简单的工具调用解析器。
        注意：Qwen3 的具体输出格式可能随版本微调，建议参考官方 Template 实现。
        这里假设模型输出了类似 <tool_call>{"name": "...", "arguments": {...}}</tool_call> 的结构
        或者直接输出 JSON。
        """
        import re
        # 示例正则：匹配 <tool_call>...</tool_call> 之间的内容
        pattern = r"<tool_call>(.*?)</tool_call>"
        matches = re.findall(pattern, text, re.DOTALL)

        if not matches:
            # 尝试匹配纯 JSON (如果模型被提示直接输出 JSON)
            try:
                # 寻找可能的 JSON 对象
                start_idx = text.find('{')
                end_idx = text.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    json_str = text[start_idx:end_idx + 1]
                    data = json.loads(json_str)
                    if 'name' in data and 'arguments' in data:
                        return [{'function': data}]
            except:
                pass
            return None

        tool_calls = []
        for match in matches:
            try:
                data = json.loads(match.strip())
                tool_calls.append({'function': data})
            except json.JSONDecodeError:
                continue
        return tool_calls if tool_calls else None

    def _generate_final_answer_stream(self, messages, tools=None):
        """
        内部方法：用于在工具执行后生成最终的自然语言回复
        """
        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            tools=tools,
            enable_thinking=False
        )

        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)
        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True)

        generation_kwargs = dict(
            inputs,
            streamer=streamer,
            max_new_tokens=32768,
            do_sample=True,
            temperature=0.7,
            top_p=0.9
        )

        thread = threading.Thread(target=self.model.generate, kwargs=generation_kwargs)
        thread.start()

        generated_text = ""
        print("Bot: ", end="", flush=True)
        for new_text in streamer:
            print(new_text, end="", flush=True)
            generated_text += new_text
        print()
        thread.join()

        # 更新历史：注意，这里应该把最初的 user input, assistant 的 tool call, tool output, 和最终的 assistant answer 都存入
        # 为简化，这里只存最后一轮完整的交互上下文可能比较复杂，建议在实际应用中维护一个完整的 message list
        # 此处仅做演示，将最后生成的回答作为 assistant 回复存入（实际应存储整个消息链）
        # 修正：应该将 messages 中的最后一条 assistant 消息更新为 generated_text，或者追加
        # 由于 messages 是局部变量，我们需要更新 self.history
        # 简单处理：清空 history 并重新构建（生产环境请优化此逻辑）
        self.history = messages[:-1]  # 保留除最后一条临时消息外的所有历史
        self.history.append({"role": "assistant", "content": generated_text})

        return generated_text


# Example Usage
if __name__ == "__main__":
    chatbot = QwenChatbot(sys_prompt="你是湘湘，是一个智能问答助手。如果用户询问天气或股票，请使用提供的工具。")

    while True:
        try:
            user_input = input("\nUser: ")
            if not user_input:
                continue
            if user_input.lower() in ['exit', 'quit']:
                break

            # 调用时传入工具定义
            response = chatbot.generate_response_stream(user_input, tools=TOOLS_SCHEMA)
            print("----------------------")
        except KeyboardInterrupt:
            print("\n退出程序")
            break
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"\n发生错误: {e}")
