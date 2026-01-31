import SwiftUI

struct SettingsView: View {
    var body: some View {
        VStack(spacing: 24) {
            AIConfigSection()
            HFModelHubSection()
            AgentPersonasSection()
            TradingConfigSection()
            AccountStatusSection()
            AboutSection()
        }
    }
}

// PREMIUM REUSABLE COMPONENTS
struct SettingsCard<Content: View>: View {
    let title: String
    let icon: String
    let content: Content
    
    init(title: String, icon: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.icon = icon
        self.content = content()
    }
    
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .foregroundColor(.accentColor)
                    .font(.system(size: 16, weight: .bold))
                Text(title.uppercased())
                    .font(.system(size: 12, weight: .black, design: .monospaced))
                    .foregroundColor(.secondary)
                Spacer()
            }
            
            content
                .padding()
                .background(Color.black.opacity(0.2))
                .cornerRadius(16)
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.secondary.opacity(0.1)))
        }
    }
}

// Data structure for exporting/importing settings
struct AppSettings: Codable {
    var selectedProvider: String
    var selectedModel: String
    var selectedCoordinatorModel: String
    var openaiApiKey: String
    var geminiApiKey: String
    var finnhubApiKey: String
    var trading212ApiKey: String
    var trading212ApiSecret: String
    var trading212IsaApiKey: String
    var trading212IsaApiSecret: String
    var alpacaApiKey: String
    var alpacaSecretKey: String
    var newsApiKey: String
    var tavilyApiKey: String
}

struct AIConfigSection: View {
    @AppStorage("selectedProvider") private var selectedProvider = "ollama"
    @AppStorage("selectedModel") private var selectedModel = "native-mlx"
    @AppStorage("selectedCoordinatorModel") private var selectedCoordinatorModel = "granite-tiny"
    @AppStorage("openaiApiKey") private var openaiApiKey = ""
    @AppStorage("geminiApiKey") private var geminiApiKey = ""
    @AppStorage("finnhubApiKey") private var finnhubApiKey = ""
    @AppStorage("trading212ApiKey") private var trading212ApiKey = ""
    @AppStorage("trading212ApiSecret") private var trading212ApiSecret = ""
    @AppStorage("trading212IsaApiKey") private var trading212IsaApiKey = ""
    @AppStorage("trading212IsaApiSecret") private var trading212IsaApiSecret = ""
    @AppStorage("alpacaApiKey") private var alpacaApiKey = ""
    @AppStorage("alpacaSecretKey") private var alpacaSecretKey = ""
    @AppStorage("newsApiKey") private var newsApiKey = ""
    @AppStorage("tavilyApiKey") private var tavilyApiKey = ""

    @State private var showExportSuccess = false
    @State private var showImportSuccess = false
    @State private var showImportError = false
    @State private var lmStudioModels: [String] = []

    var body: some View {
        SettingsCard(title: "AI Core Config", icon: "brain") {
            VStack(spacing: 20) {
                // Decision Agent Provider
                HStack {
                    Label("Reasoning Platform", systemImage: "server.rack")
                    Spacer()
                    Picker("", selection: $selectedProvider) {
                        Text("Ollama").tag("ollama")
                        Text("LM Studio").tag("lmstudio")
                        Text("OpenAI").tag("openai")
                        Text("Gemini").tag("gemini")
                        Text("MLX (Local)").tag("mlx")
                    }
                    .pickerStyle(.menu)
                    .onChange(of: selectedProvider) { _, newValue in
                        if newValue == "lmstudio" {
                            fetchLMStudioModels()
                        }
                    }
                }
                
                // Decision Agent Model
                HStack {
                    Label("Decision Engine", systemImage: "brain.head.profile")
                    Spacer()
                    Picker("", selection: $selectedModel) {
                        modelOptions
                    }
                    .pickerStyle(.menu)
                }

                Divider().background(Color.secondary.opacity(0.1))

                // Coordinator Model (Internal - Static for Stability)
                HStack {
                    Label("Expert Coordinator", systemImage: "cpu")
                    Spacer()
                    Text("IBM Granite 4.0 Tiny")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .foregroundColor(.secondary)
                }
                
                if selectedProvider == "openai" {
                    SecureField("OpenAI API Key", text: $openaiApiKey)
                        .textFieldStyle(.plain)
                        .padding(10)
                        .background(Color.secondary.opacity(0.1))
                        .cornerRadius(8)
                }
                
                if selectedProvider == "gemini" {
                    SecureField("Gemini API Key", text: $geminiApiKey)
                        .textFieldStyle(.plain)
                        .padding(10)
                        .background(Color.secondary.opacity(0.1))
                        .cornerRadius(8)
                }
                
                statusNote
            }
        }
        .onAppear {
            if selectedProvider == "lmstudio" {
                fetchLMStudioModels()
            }
        }
    }
    
    private func fetchLMStudioModels() {
        Task {
            guard let url = URL(string: "http://127.0.0.1:8002/api/models/lmstudio") else { return }
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                // Decode response: {"models": ["id1", "id2"], "error": "..."}
                if let response = try? JSONDecoder().decode([String: [String]].self, from: data),
                   let models = response["models"] {
                    await MainActor.run {
                        self.lmStudioModels = models
                        // Auto-select first if current selection is invalid
                        if !models.isEmpty && (selectedModel == "lmstudio-auto" || !models.contains(selectedModel)) {
                            // Keep 'lmstudio-auto' as a valid option or switch?
                            // Let's keep it as option, but update list
                        }
                    }
                }
            } catch {
                print("Failed to fetch LM Studio models: \(error)")
            }
        }
    }


    private var statusNote: some View {
        Group {
            if selectedProvider == "lmstudio" {
                Label("LM Studio active on port 1234", systemImage: "bolt.fill")
                    .font(.caption2)
                    .foregroundColor(.blue)
            } else if selectedProvider == "mlx" {
                Label("Hardware accelerated via Apple GPU", systemImage: "sparkles")
                    .font(.caption2)
                    .foregroundColor(.purple)
            }
        }
    }

    @ViewBuilder
    private var modelOptions: some View {
        switch selectedProvider {
        case "ollama":
            Text("Mistral").tag("mistral")
            Text("Llama 3").tag("llama3")
            Text("Gemma").tag("gemma")
        case "lmstudio":
            Text("Auto-Detect").tag("lmstudio-auto")
            ForEach(lmStudioModels, id: \.self) { model in
                Text(model).tag(model)
            }
        case "openai":
            Text("GPT-4o").tag("gpt-4o")
            Text("GPT-4 Turbo").tag("gpt-4-turbo")
        case "gemini":
            Text("Gemini 1.5 Pro").tag("gemini-1.5-pro")
            Text("Gemini 1.5 Flash").tag("gemini-1.5-flash")
        case "mlx":
            Text("LFM 2.5B (Native)").tag("native-mlx")
            Text("Mistral 7B").tag("mlx-community/Mistral-7B-v0.1-4bit-mlx")
            Text("Llama 3 8B").tag("mlx-community/Llama-3-8B-4bit-mlx")
        default:
            Text("Mistral").tag("mistral")
        }
    }
}

struct HFModelHubSection: View {
    @State private var hfSearchQuery = "mlx"
    @State private var hfModels: [HFModel] = []
    @State private var isSearching = false
    @AppStorage("selectedProvider") private var selectedProvider = "ollama"
    @AppStorage("selectedModel") private var selectedModel = "mistral"
    
    var body: some View {
        SettingsCard(title: "Model Repository", icon: "square.stack.3d.up.fill") {
            VStack(spacing: 16) {
                HStack {
                    TextField("Search HuggingFace...", text: $hfSearchQuery)
                        .textFieldStyle(.plain)
                        .padding(10)
                        .background(Color.secondary.opacity(0.1))
                        .cornerRadius(8)
                    
                    Button(action: searchHF) {
                        Image(systemName: isSearching ? "circle.dotted" : "magnifyingglass")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundColor(.primary)
                            .padding(10)
                            .background(Color.accentColor.opacity(0.2))
                            .cornerRadius(8)
                    }
                    .buttonStyle(.plain)
                }
                
                if !hfModels.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(hfModels.prefix(3)) { model in
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(model.id.components(separatedBy: "/").last ?? model.id)
                                        .font(.system(size: 12, weight: .bold, design: .monospaced))
                                    Text("\(model.downloads) downloads")
                                        .font(.system(size: 10))
                                        .foregroundColor(.secondary)
                                }
                                Spacer()
                                Button("Deploy") {
                                    selectedProvider = "mlx"
                                    selectedModel = model.id
                                }
                                .font(.system(size: 10, weight: .bold))
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(Color.accentColor)
                                .foregroundColor(.white)
                                .cornerRadius(8)
                            }
                        }
                    }
                }
            }
        }
    }
    
    private func searchHF() {
        guard !hfSearchQuery.isEmpty else { return }
        isSearching = true
        Task {
            let url = URL(string: "http://127.0.0.1:8002/models/hf/search?query=\(hfSearchQuery)")!
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                let results = try JSONDecoder().decode([HFModel].self, from: data)
                await MainActor.run {
                    self.hfModels = results
                    self.isSearching = false
                }
            } catch {
                print("HF Search error: \(error)")
                await MainActor.run { self.isSearching = false }
            }
        }
    }
}

struct AgentPersonasSection: View {
    var body: some View {
        SettingsCard(title: "Agent Matrix", icon: "person.2.fill") {
            VStack(spacing: 12) {
                PersonaToggle(title: "Portfolio Analyst", icon: "chart.pie", isOn: .constant(true))
                PersonaToggle(title: "Risk Manager", icon: "shield.fill", isOn: .constant(true))
                PersonaToggle(title: "Technical Trader", icon: "waveform.path.ecg", isOn: .constant(true))
            }
        }
    }
}

struct PersonaToggle: View {
    let title: String
    let icon: String
    @Binding var isOn: Bool
    
    var body: some View {
        HStack {
            Image(systemName: icon)
                .foregroundColor(.secondary)
                .frame(width: 20)
            Text(title)
                .font(.system(size: 13))
            Spacer()
            Toggle("", isOn: $isOn).disabled(true)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
        }
    }
}

struct TradingConfigSection: View {
    @AppStorage("t212InvestKey") private var t212InvestKey = ""
    @AppStorage("t212InvestSecret") private var t212InvestSecret = ""
    @AppStorage("t212IsaKey") private var t212IsaKey = ""
    @AppStorage("t212IsaSecret") private var t212IsaSecret = ""
    @AppStorage("t212AccountType") private var t212AccountType = "invest"
    @State private var isUpdatingConfig = false
    
    var body: some View {
        SettingsCard(title: "Trading 212 API", icon: "dollarsign.circle.fill") {
            VStack(spacing: 20) {
                Picker("", selection: $t212AccountType) {
                    Text("Invest").tag("invest")
                    Text("ISA").tag("isa")
                }
                .pickerStyle(.segmented)
                
                VStack(alignment: .leading, spacing: 12) {
                    Text("LIVE API CREDENTIALS").font(.system(size: 10, weight: .bold))
                    
                    VStack(spacing: 8) {
                        SecureField("API Key", text: $t212InvestKey)
                        SecureField("Secret", text: $t212InvestSecret)
                    }
                    .textFieldStyle(.plain)
                    .padding(10)
                    .background(Color.secondary.opacity(0.1))
                    .cornerRadius(8)
                }
                
                Button(action: updateT212Config) {
                    HStack {
                        if isUpdatingConfig {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "arrow.triangle.2.circlepath")
                            Text("UPDATE ARCHITECTURE")
                                .font(.system(size: 12, weight: .bold))
                        }
                    }
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(Color.accentColor)
                    .foregroundColor(.white)
                    .cornerRadius(12)
                }
                .buttonStyle(.plain)
            }
        }
    }
    
    private func updateT212Config() {
        isUpdatingConfig = true
        Task {
            let config: [String: Any] = [
                "account_type": t212AccountType,
                "invest_key": t212InvestKey,
                "invest_secret": t212InvestSecret,
                "isa_key": t212IsaKey,
                "isa_secret": t212IsaSecret
            ]
            
            guard let url = URL(string: "http://127.0.0.1:8002/mcp/trading212/config") else { return }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: config)
            
            do {
                let (_, _) = try await URLSession.shared.data(for: request)
                await MainActor.run { self.isUpdatingConfig = false }
            } catch {
                print("Update T212 Config error: \(error)")
                await MainActor.run { self.isUpdatingConfig = false }
            }
        }
    }
}

struct AccountStatusSection: View {
    @AppStorage("t212AccountType") private var t212AccountType = "invest"
    @StateObject private var backendStatus = BackendStatusViewModel.shared
    
    var body: some View {
        SettingsCard(title: "Connection Health", icon: "bolt.horizontal.fill") {
            VStack(spacing: 12) {
                StatusRow(label: "Active Account", value: t212AccountType.uppercased(), color: .blue)
                StatusRow(label: "Backend Server", value: backendStatus.isOnline ? "OPERATIONAL" : "OFFLINE", color: backendStatus.isOnline ? .green : .red)
            }
        }
    }
}

struct StatusRow: View {
    let label: String
    let value: String
    let color: Color
    
    var body: some View {
        HStack {
            Text(label).font(.system(size: 12))
            Spacer()
            Text(value)
                .font(.system(size: 12, weight: .black, design: .monospaced))
                .foregroundColor(color)
        }
    }
}

struct AboutSection: View {
    var body: some View {
        VStack(spacing: 8) {
            Image("Logo") // Assuming there's a logo or just a placeholder if not
                .resizable()
                .frame(width: 40, height: 40)
                .cornerRadius(10)
                .padding(.bottom, 8)
            
            Text("Growin App")
                .font(.system(size: 16, weight: .black))
            Text("v1.2.0 - ARCHITECTURE STABLE")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundColor(.secondary)
            
            Text("Designed for Professional Financial Analysis")
                .font(.system(size: 11))
                .foregroundColor(.secondary)
                .padding(.top, 4)
        }
        .padding(.vertical, 32)
        .frame(maxWidth: .infinity)
    }
}

struct MCPServer: Codable {
    let name: String
    let type: String
    let active: Bool
}

struct HFModel: Codable, Identifiable {
    let id: String
    let downloads: Int
    let likes: Int
}
