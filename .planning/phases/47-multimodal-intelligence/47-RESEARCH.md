# Phase 47: Multimodal Intelligence & Deep Integration - Research

**Researched:** 2026-06-18
**Domain:** Vision-Language Model serving, SwiftUI Deep Linking, and Dynamic Interactive UI
**Confidence:** HIGH

---

## 1. VLM Execution & mlx-vlm Integration (Gemma-4)

To support vision tasks natively on Apple Silicon (M4 Pro), we will utilize the `mlx-vlm` library.
*   **Loading**: We load via `mlx_vlm.load(model_path)` which returns `(model, processor)`. The `processor` contains both the tokenizer and the image processor.
*   **Prompt Formatting**: Gemma-4 expects a dialogue structure. According to Google prompting guidelines, the `<|image|>` token must be placed *before* the text prompt inside the user's turn block.
    Example formatted prompt:
    ```
    <|im_start|>user
    <|image|>
    Analyze this chart for entry signals.<|im_end|>
    <|im_start|>assistant
    ```
*   **Generation**: `mlx_vlm.generate(model, processor, images, prompt, ...)` performs inference. For streaming, we use `mlx_vlm.stream_generate` which yields chunk objects where `chunk.text` contains the generated text segment.
*   **Thread Safety**: Since MLX streams are thread-local, generation must be wrapped in `asyncio.to_thread` or handled safely within the async context without cross-thread execution errors.

---

## 2. Base64 Image Transmission Protocol

To keep the existing JSON Pydantic payload architecture clean and prevent multi-part boundary management overhead:
*   SwiftUI encodes selected images to JPEG data, converts them to Base64 strings, and submits them inline within the `ChatMessage` request payload under the `images` array field.
*   The FastAPI backend parses the `images` array from the Pydantic `ChatMessage` model.
*   The backend decodes each Base64 string to a `PIL.Image` object:
    ```python
    import base64
    from io import BytesIO
    from PIL import Image
    
    def decode_base64_image(base64_str: str) -> Image.Image:
        image_data = base64.b64decode(base64_str.split(",")[-1]) # strip data uri prefix if present
        return Image.open(BytesIO(image_data)).convert("RGB")
    ```

---

## 3. SwiftUI Deep Linking & openURL Interception

SwiftUI's standard `Text` view has built-in Markdown support (e.g. `[AAPL](growin://ledger/AAPL)`). However, on macOS, `.textSelection(.enabled)` often intercepts and blocks link click events.
*   **Mitigation**: We attach `.environment(\.openURL, ...)` in `ThemeComponents.swift` (on the `MarkdownText` renderer) to capture clicks locally before they propagate to the OS.
*   **Implementation**:
    ```swift
    Text(LocalizedStringKey(parsed.cleanContent))
        .environment(\.openURL, OpenURLAction { url in
            if url.scheme == "growin" {
                if url.host == "ledger", let ticker = url.path.split(separator: "/").first {
                    NotificationCenter.default.post(
                        name: NSNotification.Name("DeepLinkTicker"),
                        object: nil,
                        userInfo: ["ticker": String(ticker)]
                    )
                    return .handled
                }
            }
            return .systemAction
        })
    ```
*   This triggers the `DeepLinkTicker` listener in `GrowinApp.swift` or `ContentView.swift`, which navigates the user to the respective Sovereign Ledger view.

---

## 4. SSE-driven Real-Time Interactive Tiles

*   **Data Binding**: SwiftUI chat message bubble views can dynamically render `InlineActionTile` components when the assistant response contains trade proposals or ticker updates.
*   **SSE Binding**: Bind custom dynamic tiles to `PortfolioSummaryObserver` or `DashboardViewModel` to listen to price updates or SSE stream events directly, ensuring sub-16ms rendering updates on 120Hz ProMotion screens without establishing duplicate WebSocket channels.

---

## 5. Next Steps
1. Extend `MLXInferenceEngine` to support `mlx-vlm` models.
2. Extend `ChatMessage` and SQLite `messages` table to persist and load Base64 images.
3. Enhance `ChatMLX` and `DecisionAgent` to structure and forward images to the inference loop.
4. Implement the UI image upload picker, preview bar, and backend transmission client.
5. Apply local link interception and dynamic interactive tiles in the SwiftUI interface.

---
*Phase: 47-multimodal-intelligence*
*Research updated: 2026-06-18*
