import SwiftUI
import AppKit

struct SettingsView: View {
    var body: some View {
        VStack(spacing: 24) {
            AIConfigSection()
            HFModelHubSection()
            AgentPersonasSection()
            ApprovalSecuritySection()
            TradingConfigSection()
            AccountStatusSection()
            AboutSection()
        }
    }
}

struct ApprovalSecuritySection: View {
    /// Empty means no workspace is selected. Nothing is preselected.
    @AppStorage(WorkspaceSelection.defaultsKey) private var selectedWorkspaceRaw = ""
    @State private var isEnrolling = false
    @State private var isRunningCheck = false
    @State private var isRunningRequoteCheck = false
    @State private var statusMessage: String?
    @State private var statusIsError = false
    @State private var pendingReview: TradeApprovalReview?
    @State private var pendingRequoteReview: TradeApprovalReview?

    private var workspace: Workspace? {
        Workspace(rawValue: selectedWorkspaceRaw)
    }

    private var isSelectedKeyConfigured: Bool {
        guard let workspace else { return false }
        return LocalApprovalSigner.shared.isConfigured(for: workspace)
    }

    private var canAdoptLegacyKey: Bool {
        guard let workspace else { return false }
        return LocalApprovalSigner.shared.canAdoptLegacyKey(into: workspace)
    }

    /// Resolves the selected workspace and the signing identity that must match
    /// the review before anything is signed.
    private func signingContext(for review: TradeApprovalReview) throws -> Workspace {
        guard let workspace else {
            throw ChatWorkspaceError.noWorkspaceSelected
        }
        guard review.payload.workspace == workspace.rawValue else {
            throw TradeApprovalReviewError.workspaceMismatch
        }
        let identity = try LocalApprovalSigner.shared.identity(for: workspace)
        guard identity.keyID == review.payload.keyId else {
            throw TradeApprovalReviewError.signerMismatch
        }
        return workspace
    }

    var body: some View {
        SettingsCard(title: "Trade Approval Security", icon: "key.fill") {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Workspace", selection: $selectedWorkspaceRaw) {
                    Text("Select a workspace").tag("")
                    ForEach(Workspace.allCases, id: \.self) { workspace in
                        Text(workspace.displayName).tag(workspace.rawValue)
                    }
                }
                .pickerStyle(.menu)

                if let workspace {
                    Label(
                        isSelectedKeyConfigured
                            ? "\(workspace.displayName) signing key present in Keychain"
                            : "Local paper approval is not configured for \(workspace.displayName)",
                        systemImage: isSelectedKeyConfigured
                            ? "checkmark.shield.fill"
                            : "shield.slash"
                    )
                    .foregroundStyle(isSelectedKeyConfigured ? .green : .secondary)
                } else {
                    Label("Select a workspace to manage its approval key", systemImage: "shield.slash")
                        .foregroundStyle(.secondary)
                }

                Text("Paper orders require an explicit frozen review and a signature over those exact fields. Each workspace has its own private key in this Mac's Keychain. Live execution remains disabled.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text("This free local mode prevents stale, altered, or replayed approvals, but it does not provide Touch ID or Secure Enclave isolation.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if let statusMessage {
                    Text(statusMessage)
                        .font(.caption)
                        .foregroundStyle(statusIsError ? .red : .green)
                }

                Button { enroll() } label: {
                    if isEnrolling {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Set up local paper approvals", systemImage: "key.fill")
                    }
                }
                .disabled(workspace == nil || isEnrolling)

                if canAdoptLegacyKey {
                    Button { adoptLegacyKey() } label: {
                        Label("Adopt existing local key for UK", systemImage: "arrow.down.to.line")
                    }
                    .disabled(isEnrolling)
                    .help("Moves the pre-workspace key into UK only if it matches the key the UK ledger enrolled.")
                }

                Button { runPaperApprovalCheck() } label: {
                    if isRunningCheck {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Run paper approval check", systemImage: "checkmark.seal")
                    }
                }
                .disabled(workspace == nil || isRunningCheck || isEnrolling)
                .help("Creates one local-only paper proposal, then opens the frozen review sheet.")

                Button { runPaperRequoteCheck() } label: {
                    if isRunningRequoteCheck {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Run paper replacement UAT", systemImage: "arrow.triangle.2.circlepath")
                    }
                }
                .disabled(workspace == nil || isRunningCheck || isRunningRequoteCheck || isEnrolling)
                .help("Creates a local cancelled parent and fresh LIMIT replacement, then verifies a signature without dispatching.")
            }
        }
        .sheet(item: $pendingReview) { review in
            TradeApprovalSheet(review: review) {
                let ws = try signingContext(for: review)
                let signature = try LocalApprovalSigner.shared.sign(review.signedBytes, for: ws)
                _ = try await AIService().completeTradeApproval(review, signature: signature, workspace: ws)
                statusIsError = false
                statusMessage = "Paper approval check acknowledged locally. No broker was contacted."
            }
        }
        .sheet(item: $pendingRequoteReview) { review in
            TradeApprovalSheet(
                review: review,
                title: "Paper replacement verification",
                explanation: "This is a local cancelled-parent replacement. Your key signs the fresh frozen LIMIT fields, but this UAT only verifies the signature: it cannot dispatch an order.",
                approveTitle: "Sign and verify locally"
            ) {
                let ws = try signingContext(for: review)
                let signature = try LocalApprovalSigner.shared.sign(review.signedBytes, for: ws)
                _ = try await AIService().verifyPaperRequoteCheck(review, signature: signature, workspace: ws)
                statusIsError = false
                statusMessage = "Paper replacement signature verified locally. No order was dispatched and no broker was contacted."
            }
        }
    }

    private func enroll() {
        guard let ws = workspace else { return }
        isEnrolling = true
        statusMessage = nil
        Task {
            do {
                let identity = try LocalApprovalSigner.shared.createIdentityIfNeeded(for: ws)
                let approvalStatus = try await AIService().approvalStatus(workspace: ws)
                if approvalStatus.enrolled {
                    guard approvalStatus.keyId == identity.keyID else {
                        throw TradeApprovalReviewError.signerMismatch
                    }
                    statusIsError = false
                    statusMessage = "Local paper approval is already enrolled for the \(ws.displayName) workspace."
                    isEnrolling = false
                    return
                }
                guard approvalStatus.mode == "paper" else {
                    throw NSError(domain: "Growin.Approval", code: 1,
                                  userInfo: [NSLocalizedDescriptionKey: "Local paper execution is unavailable."])
                }
                let tokenURL = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent("Library/Application Support/Growin/workspaces/\(ws.rawValue)/execution.sqlite3.enrollment-token")
                let token = try String(contentsOf: tokenURL, encoding: .utf8)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                _ = try await AIService().enrollApprovalKey(identity: identity, token: token, workspace: ws)
                statusIsError = false
                statusMessage = "Local paper approval is enrolled for the \(ws.displayName) workspace."
            } catch {
                statusIsError = true
                statusMessage = error.localizedDescription
            }
            isEnrolling = false
        }
    }

    private func adoptLegacyKey() {
        isEnrolling = true
        statusMessage = nil
        Task {
            do {
                let approvalStatus = try await AIService().approvalStatus(workspace: .uk)
                guard approvalStatus.enrolled, let enrolledKeyID = approvalStatus.keyId else {
                    throw LocalApprovalSignerError.legacyKeyMismatch
                }
                _ = try LocalApprovalSigner.shared.adoptLegacyKey(into: .uk, expectedKeyID: enrolledKeyID)
                statusIsError = false
                statusMessage = "The existing local key now belongs to the UK workspace."
            } catch {
                statusIsError = true
                statusMessage = error.localizedDescription
            }
            isEnrolling = false
        }
    }

    private func runPaperApprovalCheck() {
        guard let ws = workspace else { return }
        isRunningCheck = true
        statusMessage = nil
        Task {
            do {
                let service = AIService()
                let proposal = try await service.createPaperApprovalCheck(workspace: ws)
                pendingReview = try await service.requestTradeApproval(proposal: proposal, workspace: ws)
            } catch {
                statusIsError = true
                statusMessage = error.localizedDescription
            }
            isRunningCheck = false
        }
    }

    private func runPaperRequoteCheck() {
        guard let ws = workspace else { return }
        isRunningRequoteCheck = true
        statusMessage = nil
        Task {
            do {
                let service = AIService()
                let proposal = try await service.createPaperRequoteCheck(workspace: ws)
                pendingRequoteReview = try await service.requestTradeApproval(proposal: proposal, workspace: ws)
            } catch {
                statusIsError = true
                statusMessage = error.localizedDescription
            }
            isRunningRequoteCheck = false
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
                .clipShape(.rect(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.secondary.opacity(0.1)))
        }
    }
}

struct AIConfigSection: View {
    @State private var roles: ModelRolesResponse?
    @State private var isLoading = false

    var body: some View {
        SettingsCard(title: "AI Core Config", icon: "brain") {
            VStack(spacing: 14) {
                if let roles {
                    ForEach(roles.roles) { role in
                        roleRow(role)
                    }
                    if !roles.missingRoles.isEmpty {
                        Label("Not configured: \(roles.missingRoles.joined(separator: ", "))", systemImage: "exclamationmark.triangle")
                            .font(.caption2)
                            .foregroundColor(.stitchNeonYellow)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else if isLoading {
                    ProgressView().controlSize(.small)
                } else {
                    Label("Model registry unavailable. AI features answer 503 until a valid private/models.json is loaded.", systemImage: "bolt.slash.fill")
                        .font(.caption2)
                        .foregroundColor(.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                Divider().background(Color.secondary.opacity(0.1))

                HStack {
                    Text("Models are set in private/models.json on the backend, not in the app.")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                    Spacer()
                    Button("Refresh") {
                        Task { await load() }
                    }
                    .font(.system(size: 10, weight: .bold))
                    .accessibilityLabel("Refresh model roles")
                    .accessibilityHint("Reloads the model roles from the backend")
                }
            }
        }
        .task { await load() }
    }

    private func roleRow(_ role: ModelRole) -> some View {
        HStack {
            Label(role.role.replacingOccurrences(of: "_", with: " ").capitalized, systemImage: "cpu")
            Spacer()
            if role.keyConfigured == false {
                Text("Key missing")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundColor(.red)
            }
            Text("\(role.provider) / \(role.model)")
                .font(.system(size: 13, weight: .bold, design: .monospaced))
                .foregroundColor(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    private func load() async {
        isLoading = true
        roles = await AgentClient().fetchModelRoles()
        isLoading = false
    }
}

struct HFModelHubSection: View {
    @State private var hfSearchQuery = "mlx"
    @State private var hfModels: [HFModel] = []
    @State private var isSearching = false
    
    var body: some View {
        SettingsCard(title: "Model Repository", icon: "square.stack.3d.up.fill") {
            VStack(spacing: 16) {
                HStack {
                    TextField("Search HuggingFace...", text: $hfSearchQuery)
                        .textFieldStyle(.plain)
                        .padding(10)
                        .background(Color.secondary.opacity(0.1))
                        .clipShape(.rect(cornerRadius: 8))
                        .accessibilityLabel("Search HuggingFace Models")
                        .accessibilityHint("Enter a model name to search on HuggingFace")
                    
                    Button(action: searchHF) {
                        Image(systemName: isSearching ? "circle.dotted" : "magnifyingglass")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundColor(.primary)
                            .padding(10)
                            .background(Color.accentColor.opacity(0.2))
                            .clipShape(.rect(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Search Models")
                    .accessibilityHint("Searches HuggingFace for the specified model")
                    .accessibilityAddTraits(.isButton)
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
                                Button("Copy ID") {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(model.id, forType: .string)
                                }
                                .font(.system(size: 10, weight: .bold))
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(Color.accentColor)
                                .foregroundColor(.white)
                                .clipShape(.rect(cornerRadius: 8))
                                .accessibilityLabel("Copy \(model.id)")
                                .accessibilityHint("Copies the model id so it can be set in private/models.json")
                                .accessibilityAddTraits(.isButton)
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
            let url = URL(string: "\(AppConfig.shared.baseURL)/models/hf/search?query=\(hfSearchQuery)")!
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                let results = try JSONDecoder().decode([HFModel].self, from: data)
                self.hfModels = results
                self.isSearching = false
            } catch {
                print("HF Search error: \(error)")
                self.isSearching = false
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
            Toggle(title, isOn: $isOn).disabled(true)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
                .accessibilityLabel("\(title) Persona")
                .accessibilityHint("Toggles the \(title) agent persona")
        }
    }
}

struct TradingConfigSection: View {
    @AppStorage("t212AccountType") private var t212AccountType = "invest"
    
    var body: some View {
        SettingsCard(title: "Trading 212 API", icon: "dollarsign.circle.fill") {
            VStack(spacing: 20) {
                Picker("Trading 212 Account Type", selection: $t212AccountType) {
                    Text("Invest").tag("invest")
                    Text("ISA").tag("isa")
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .accessibilityLabel("Trading 212 Account Type")
                .accessibilityHint("Selects between Invest and ISA account types")
                
                VStack(alignment: .leading, spacing: 8) {
                    Text("API CREDENTIALS").font(.system(size: 10, weight: .bold))
                    Text("Trading 212 keys come from the backend's launch environment. This app never sends them to the backend.")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel("Trading 212 keys come from the backend's launch environment")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(Color.secondary.opacity(0.1))
                .clipShape(.rect(cornerRadius: 8))
            }
        }
    }
}

struct AccountStatusSection: View {
    @AppStorage("t212AccountType") private var t212AccountType = "invest"
    @State private var backendStatus = BackendStatusViewModel.shared
    
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
                .clipShape(.rect(cornerRadius: 10))
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
