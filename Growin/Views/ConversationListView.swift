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

@Observable @MainActor
class ConversationListViewModel {
    var conversations: [ConversationItem] = []
    var isLoading = false
    var errorMessage: String?

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
                _ = try await URLSession.shared.data(for: request)
            } catch {
                print("Error deleting conversation \(id): \(error)")
            }
        }

        await fetchConversations()
    }
}

struct ConversationListView: View {
    @State private var viewModel = ConversationListViewModel()
    @Binding var selectedConversationId: String?
    @Environment(\.dismiss) private var dismiss

    @State private var isEditing = false
    @State private var selectedIds: Set<String> = []
    @State private var showDeleteConfirmation = false

    var selectAllButtonTitle: String {
        selectedIds.count == viewModel.conversations.count ? "Deselect All" : "Select All"
    }

    var groupedConversations: [(String, [ConversationItem])] {
        if viewModel.conversations.isEmpty { return [] }
        
        let grouped = Dictionary(grouping: viewModel.conversations) { conversation -> String in
            guard let date = ISO8601DateFormatter().date(from: conversation.createdAt) else {
                return "Older"
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
        return order.compactMap { key in
            guard let items = grouped[key] else { return nil }
            return (key, items.sorted { $0.createdAt > $1.createdAt })
        }
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                
                VStack(spacing: 0) {
                    if viewModel.isLoading && viewModel.conversations.isEmpty {
                        VStack(spacing: 12) {
                            ProgressView().tint(.white)
                            Text("Loading protocols...").font(.caption).foregroundStyle(.secondary)
                        }
                        .frame(maxHeight: .infinity)
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 16) {
                            Image(systemName: "wifi.exclamationmark").font(.largeTitle).foregroundStyle(.red)
                            Text(error).font(.system(size: 13, design: .monospaced)).multilineTextAlignment(.center).padding(.horizontal)
                            Button("Retry Connection") { Task { await viewModel.fetchConversations() } }.buttonStyle(.plain).padding(10).background(Color.white.opacity(0.1)).clipShape(Capsule())
                        }
                        .frame(maxHeight: .infinity)
                    } else if viewModel.conversations.isEmpty {
                        ContentUnavailableView("No Conversations", systemImage: "bubble.left.and.bubble.right", description: Text("Start chatting to see your history"))
                            .frame(maxHeight: .infinity)
                    } else {
                        // SOTA: Use ScrollView + LazyVStack instead of List for better macOS rendering
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 12, pinnedViews: [.sectionHeaders]) {
                                ForEach(groupedConversations, id: \.0) { sectionTitle, conversations in
                                    Section(header: 
                                        HStack {
                                            Text(sectionTitle)
                                                .font(.system(size: 11, weight: .bold))
                                                .foregroundStyle(.secondary)
                                                .padding(.vertical, 4)
                                            Spacer()
                                        }
                                        .padding(.horizontal, 16)
                                        .background(Color.black.opacity(0.8))
                                    ) {
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
                                            .padding(.horizontal, 16)
                                        }
                                    }
                                }
                            }
                            .padding(.vertical, 16)
                        }
                    }
                }
            }
            .navigationTitle("Conversations")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(action: { dismiss() }) {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.gray.opacity(0.8))
                    }
                }

                ToolbarItem {
                    if isEditing {
                        Button("Cancel") { isEditing = false; selectedIds.removeAll() }.foregroundStyle(.white)
                    } else {
                        HStack {
                            Button(action: { Task { await viewModel.fetchConversations() } }) { Image(systemName: "arrow.clockwise") }
                            Button(action: { selectedConversationId = nil; dismiss() }) { Image(systemName: "plus") }
                        }
                    }
                }

                ToolbarItem {
                    HStack {
                        if isEditing {
                            Button(selectAllButtonTitle) { toggleSelectAll() }.foregroundStyle(.white)
                            if !selectedIds.isEmpty {
                                Button(role: .destructive) { showDeleteConfirmation = true } label: {
                                    HStack { Image(systemName: "trash"); Text("Delete All (\(selectedIds.count))") }.foregroundStyle(.red)
                                }
                            }
                        }
                        Button(isEditing ? "Done" : "Edit") {
                            isEditing.toggle()
                            if !isEditing { selectedIds.removeAll() }
                        }.foregroundStyle(.white)
                    }
                }
            }
            .task { await viewModel.fetchConversations() }
            .alert("Delete \(selectedIds.count) Conversations?", isPresented: $showDeleteConfirmation) {
                Button("Cancel", role: .cancel) { }
                Button("Delete", role: .destructive) {
                    Task {
                        await viewModel.deleteConversations(ids: Array(selectedIds))
                        isEditing = false; selectedIds.removeAll()
                    }
                }
            }
        }
    }

    func toggleSelectAll() {
        if selectedIds.count == viewModel.conversations.count { selectedIds.removeAll() }
        else { selectedIds = Set(viewModel.conversations.map { $0.id }) }
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
                        .font(.system(size: 18))
                }

                GlassCard(cornerRadius: 12) {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(conversation.title ?? "Untitled Conversation")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundStyle(.white)
                                .lineLimit(1)
                            Spacer()
                            Text(formatDate(conversation.createdAt))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }

                        if let preview = conversation.lastMessage {
                            Text(preview)
                                .font(.system(size: 12))
                                .foregroundStyle(.white.opacity(0.6))
                                .lineLimit(1)
                        }
                    }
                    .padding(12)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(conversation.title ?? "Untitled Conversation")
        .accessibilityHint("Double tap to \(isEditing ? "select" : "open") conversation")
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }

    private func formatDate(_ dateString: String) -> String {
        let isoFormatter = ISO8601DateFormatter()
        let isoFormatterWithFractional = ISO8601DateFormatter()
        isoFormatterWithFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        
        let date = isoFormatter.date(from: dateString) ?? isoFormatterWithFractional.date(from: dateString)
        
        if let validDate = date {
            let relativeFormatter = RelativeDateTimeFormatter()
            relativeFormatter.unitsStyle = .abbreviated
            return relativeFormatter.localizedString(for: validDate, relativeTo: Date())
        }
        return dateString
    }
}
