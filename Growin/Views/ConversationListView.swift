import Combine
import SwiftUI

struct ConversationItem: Codable, Identifiable {
    let id: String
    let createdAt: String
    let title: String?
    var lastMessage: String?

    enum CodingKeys: String, CodingKey {
        case id
        case createdAt = "created_at"
        case title
        case lastMessage = "last_message"
    }
}

class ConversationListViewModel: ObservableObject {
    @Published var conversations: [ConversationItem] = []
    @Published var isLoading = false
    @Published var errorMessage: String?

    private let config = AppConfig.shared
    private var cancellables = Set<AnyCancellable>()

    init() {
        NotificationCenter.default.publisher(for: NSNotification.Name("RefreshConversations"))
            .sink { [weak self] _ in
                Task {
                    await self?.fetchConversations()
                }
            }
            .store(in: &cancellables)
    }

    @MainActor
    func fetchConversations() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        guard let url = URL(string: "\(AppConfig.shared.baseURL)/conversations") else {
            errorMessage = "Invalid URL"
            return
        }

        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            
            // Debug: Print raw data if needed (optional)
            // if let jsonString = String(data: data, encoding: .utf8) { print("Conversations JSON: \(jsonString)") }

            let decoded = try JSONDecoder().decode([ConversationItem].self, from: data)
            self.conversations = decoded
        } catch {
            self.errorMessage = "Sync Error: \(error.localizedDescription)"
            print("❌ Failed to fetch conversations: \(error)")
        }
    }

    @MainActor
    func deleteConversations(ids: [String]) async {
        isLoading = true
        defer { isLoading = false }

        for id in ids {
            guard let url = URL(string: "\(config.baseURL)/conversations/\(id)") else { continue }
            var request = URLRequest(url: url)
            request.httpMethod = "DELETE"

            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                    print("Deleted conversation: \(id)")
                } else {
                    let errorBody = String(data: data, encoding: .utf8) ?? "no body"
                    print("Failed to delete conversation \(id): \(errorBody)")
                }
            } catch {
                print("Error deleting conversation \(id): \(error)")
            }
        }

        await fetchConversations()
    }
}

struct ConversationListView: View {
    @StateObject private var viewModel = ConversationListViewModel()
    @Binding var selectedConversationId: String?
    @Environment(\.dismiss) private var dismiss

    @State private var isEditing = false
    @State private var selectedIds: Set<String> = []
    @State private var showDeleteConfirmation = false

    var selectAllButtonTitle: String {
        selectedIds.count == viewModel.conversations.count ? "Deselect All" : "Select All"
    }

    // Group conversations by date
    var groupedConversations: [(String, [ConversationItem])] {
        if viewModel.conversations.isEmpty { return [] }
        
        let grouped = Dictionary(grouping: viewModel.conversations) { conversation -> String in
            guard let date = ISO8601DateFormatter().date(from: conversation.createdAt) else {
                return "Older" // Fallback for unparseable dates
            }
            
            if Calendar.current.isDateInToday(date) {
                return "Today"
            } else if Calendar.current.isDateInYesterday(date) {
                return "Yesterday"
            } else if Calendar.current.isDate(date, equalTo: Date(), toGranularity: .weekOfYear) {
                return "This Week"
            } else {
                return "Older"
            }
        }

        let order = ["Today", "Yesterday", "This Week", "Older"]
        var sections: [(String, [ConversationItem])] = order.compactMap { (key: String) -> (String, [ConversationItem])? in
            guard let items = grouped[key] else { return nil }
            return (key, items.sorted { $0.createdAt > $1.createdAt })
        }
        
        // Catch any items that didn't fit in the defined order (paranoia check)
        let handledIds = Set(sections.flatMap { $0.1.map { $0.id } })
        let unhandled = viewModel.conversations.filter { !handledIds.contains($0.id) }
        if !unhandled.isEmpty {
            sections.append(("Other", unhandled.sorted { $0.createdAt > $1.createdAt }))
        }
        
        return sections
    }

    func toggleSelectAll() {
        if selectedIds.count == viewModel.conversations.count {
            selectedIds.removeAll()
        } else {
            selectedIds = Set(viewModel.conversations.map { $0.id })
        }
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                
                VStack(spacing: 0) {
                    if viewModel.isLoading && viewModel.conversations.isEmpty {
                        VStack(spacing: 12) {
                            ProgressView()
                                .tint(.white)
                            Text("Loading protocols...")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 16) {
                            Image(systemName: "wifi.exclamationmark")
                                .font(.largeTitle)
                                .foregroundStyle(.red)
                            Text(error)
                                .font(.system(size: 13, design: .monospaced))
                                .multilineTextAlignment(.center)
                                .padding(.horizontal)
                            
                            Button("Retry Connection") {
                                Task { await viewModel.fetchConversations() }
                            }
                            .buttonStyle(.plain)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .background(Color.white.opacity(0.1))
                            .clipShape(Capsule())
                        }
                    } else if viewModel.conversations.isEmpty {
                        ContentUnavailableView {
                            Label("No Conversations", systemImage: "bubble.left.and.bubble.right")
                        } description: {
                            Text("Start chatting to see your conversation history here")
                        }
                    } else {
                        List {
                            ForEach(groupedConversations, id: \.0) { sectionTitle, conversations in
                                Section(header: Text(sectionTitle).foregroundStyle(.secondary)) {
                                    ForEach(conversations) { conversation in
                                        ConversationCard(
                                            conversation: conversation,
                                            isSelected: selectedIds.contains(conversation.id),
                                            isEditing: isEditing
                                        ) {
                                            if isEditing {
                                                if selectedIds.contains(conversation.id) {
                                                    selectedIds.remove(conversation.id)
                                                } else {
                                                    selectedIds.insert(conversation.id)
                                                }
                                            } else {
                                                selectedConversationId = conversation.id
                                                dismiss()
                                            }
                                        }
                                        .listRowBackground(Color.clear)
                                        .listRowSeparator(.hidden)
                                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                            Button(role: .destructive) {
                                                Task {
                                                    await viewModel.deleteConversations(ids: [conversation.id])
                                                }
                                            } label: {
                                                Label("Delete", systemImage: "trash")
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        .listStyle(.plain)
                        .scrollContentBackground(.hidden)
                        .refreshable {
                            await viewModel.fetchConversations()
                        }
                    }
                }
            }
            .navigationTitle("Conversations")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(action: { dismiss() }) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.gray.opacity(0.8))
                    }
                }

                ToolbarItem {
                    if isEditing {
                        Button("Cancel") {
                            isEditing = false
                            selectedIds.removeAll()
                        }
                        .foregroundStyle(.white)
                    } else {
                        HStack {
                            Button(action: {
                                Task { await viewModel.fetchConversations() }
                            }) {
                                Image(systemName: "arrow.clockwise")
                            }
                            
                            Button(action: {
                                selectedConversationId = nil
                                dismiss()
                            }) {
                                Image(systemName: "plus")
                            }
                        }
                    }
                }

                ToolbarItem {
                    HStack {
                        if isEditing {
                            // Select All / Deselect All button
                            Button(selectAllButtonTitle) {
                                toggleSelectAll()
                            }
                            .foregroundStyle(.white)

                            // Delete All button
                            if !selectedIds.isEmpty {
                                Button(role: .destructive) {
                                    showDeleteConfirmation = true
                                } label: {
                                    HStack {
                                        Image(systemName: "trash")
                                        Text("Delete All (\(selectedIds.count))")
                                    }
                                    .foregroundStyle(.red)
                                }
                            }
                        }

                        Button(isEditing ? "Done" : "Edit") {
                            isEditing.toggle()
                            if !isEditing {
                                selectedIds.removeAll()
                            }
                        }
                        .foregroundStyle(.white)
                    }
                }
            }
            .task {
                await viewModel.fetchConversations()
            }
            .onAppear {
                Task { await viewModel.fetchConversations() }
            }
            .alert("Delete \(selectedIds.count) Conversation\(selectedIds.count == 1 ? "" : "s")?", isPresented: $showDeleteConfirmation) {
                Button("Cancel", role: .cancel) { }
                Button("Delete", role: .destructive) {
                    Task {
                        await viewModel.deleteConversations(ids: Array(selectedIds))
                        isEditing = false
                        selectedIds.removeAll()
                        showDeleteConfirmation = false
                    }
                }
            } message: {
                Text("This action cannot be undone. All selected conversations will be permanently deleted.")
            }
        }
    }
}

struct ConversationCard: View {
    let conversation: ConversationItem
    let isSelected: Bool
    let isEditing: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 12) {
                if isEditing {
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(isSelected ? Color.blue : Color.secondary)
                        .font(.system(size: 20))
                }

                GlassCard(cornerRadius: 12) {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text(conversation.title ?? "Untitled Conversation")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(.white)
                                .lineLimit(1)

                            Spacer()

                            Text(formatDate(conversation.createdAt))
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }

                        if let preview = conversation.lastMessage {
                            Text(preview)
                                .font(.system(size: 13))
                                .foregroundStyle(.white.opacity(0.7))
                                .lineLimit(2)
                                .multilineTextAlignment(.leading)
                        }
                    }
                    .padding(14)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabelString)
        .accessibilityHint(isEditing ? "Double tap to toggle selection" : "Double tap to open conversation")
        .accessibilityAddTraits(isEditing && isSelected ? [.isSelected] : [])
    }

    private var accessibilityLabelString: String {
        let date = formatDate(conversation.createdAt)
        let title = conversation.title ?? "Untitled Conversation"

        if isEditing {
            return "Select \(title), created \(date)"
        } else {
            let preview = conversation.lastMessage ?? "No preview"
            return "Conversation: \(title). Created \(date). Last message: \(preview)"
        }
    }

    private func formatDate(_ dateString: String) -> String {
        // Try multiple parsers for robustness
        let isoFormatter = ISO8601DateFormatter()
        let isoFormatterWithFractional = ISO8601DateFormatter()
        isoFormatterWithFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        
        var date: Date? = isoFormatter.date(from: dateString)
        if date == nil {
            date = isoFormatterWithFractional.date(from: dateString)
        }
        
        if let validDate = date {
            let relativeFormatter = RelativeDateTimeFormatter()
            relativeFormatter.unitsStyle = .abbreviated
            return relativeFormatter.localizedString(for: validDate, relativeTo: Date())
        }
        
        // Fallback to simple date parsing if ISO fails
        let simpleFormatter = DateFormatter()
        simpleFormatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        if let fallbackDate = simpleFormatter.date(from: dateString) {
            let relativeFormatter = RelativeDateTimeFormatter()
            relativeFormatter.unitsStyle = .abbreviated
            return relativeFormatter.localizedString(for: fallbackDate, relativeTo: Date())
        }
        
        return dateString
    }
}

#Preview {
    ConversationListView(selectedConversationId: .constant(nil))
}
