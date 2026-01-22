import SwiftUI

struct ChartsView: View {
    @State private var searchText = ""
    @State private var selectedSymbol: String? = "AAPL" // Default to AAPL for a good first impression
    @State private var searchResults: [SearchResult] = []
    @State private var isSearching = false
    @State private var chartViewModel: StockChartViewModel? = nil
    @FocusState private var isSearchFocused: Bool
    
    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            
            VStack(spacing: 0) {
                // PREMIUM SEARCH HEADER
                VStack(spacing: 16) {
                    HStack {
                        Text("Market Explorer")
                            .font(.system(size: 24, weight: .black))
                            .foregroundStyle(.white)
                        Spacer()
                    }
                    
                    searchField
                }
                .padding()
                .background(Color.black.opacity(0.3))
                
                // CONTENT AREA
                if isSearching && searchResults.isEmpty {
                    ProgressView()
                        .frame(maxHeight: .infinity)
                } else if !searchResults.isEmpty {
                    suggestionsList
                } else if let symbol = selectedSymbol {
                    ScrollView {
                        VStack(spacing: 24) {
                            StockChartView(viewModel: StockChartViewModel(symbol: symbol))
                                .padding()
                            
                            // Advanced Features - REAL ANALYSIS
                            HStack(spacing: 16) {
                                GlassCard {
                                    VStack(alignment: .leading, spacing: 8) {
                                        HStack {
                                            Text("AI ANALYSIS")
                                                .font(.system(size: 10, weight: .black, design: .monospaced))
                                                .foregroundColor(.blue)
                                            
                                            Spacer()
                                            
                                            if let vm = chartViewModel, let updated = vm.lastUpdated {
                                                Text(updated, style: .relative)
                                                    .font(.system(size: 8))
                                                    .foregroundColor(.secondary)
                                            }
                                        }
                                        
                                        Text(chartViewModel?.aiAnalysis ?? "Loading analysis...")
                                            .font(.system(size: 11))
                                            .foregroundColor(.white.opacity(0.8))
                                    }
                                }
                                
                                GlassCard {
                                    VStack(alignment: .leading, spacing: 8) {
                                        HStack {
                                            Text("ALGO SIGNALS")
                                                .font(.system(size: 10, weight: .black, design: .monospaced))
                                                .foregroundColor(.green)
                                            
                                            Spacer()
                                            
                                            if let vm = chartViewModel, let updated = vm.lastUpdated {
                                                Text(updated, style: .relative)
                                                    .font(.system(size: 8))
                                                    .foregroundColor(.secondary)
                                            }
                                        }
                                        
                                        Text(chartViewModel?.algoSignals ?? "Loading signals...")
                                            .font(.system(size: 11))
                                            .foregroundColor(.white.opacity(0.8))
                                    }
                                }
                            }
                            .padding(.horizontal)
                        }
                    }
                    .onAppear {
                        if chartViewModel == nil || chartViewModel?.symbol != symbol {
                            chartViewModel = StockChartViewModel(symbol: symbol)
                        }
                    }
                } else {
                    emptyState
                }
            }
        }
    }
    
    private var searchField: some View {
        HStack {
            Image(systemName: "magnifyingglass")
                .foregroundColor(.secondary)
            
            TextField("Search Ticker (e.g. VOD.L, TSLA, BTC/USD)", text: $searchText)
                .textFieldStyle(.plain)
                .focused($isSearchFocused)
                .onSubmit {
                    performSearch()
                }
                .onChange(of: searchText) { _, newValue in
                    if newValue.count >= 2 {
                        performSearch()
                    } else if newValue.isEmpty {
                        searchResults = []
                    }
                }
            
            if !searchText.isEmpty {
                Button(action: { 
                    searchText = ""
                    searchResults = []
                    isSearchFocused = false
                }) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.05))
        .cornerRadius(12)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(isSearchFocused ? Color.blue.opacity(0.5) : Color.clear, lineWidth: 1)
        )
    }
    
    private var suggestionsList: some View {
        List(searchResults) { result in
            Button(action: {
                withAnimation {
                    selectedSymbol = result.ticker
                    searchText = ""
                    searchResults = []
                    isSearchFocused = false
                }
            }) {
                HStack(spacing: 16) {
                    Text(result.ticker)
                        .font(.system(size: 14, weight: .black, design: .monospaced))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Color.blue.opacity(0.2))
                        .foregroundColor(.blue)
                        .cornerRadius(4)
                    
                    VStack(alignment: .leading) {
                        Text(result.name)
                            .font(.system(size: 14, weight: .bold))
                        Text("Global Exchange")
                            .font(.system(size: 10))
                            .foregroundColor(.secondary)
                    }
                    
                    Spacer()
                    
                    Image(systemName: "arrow.up.right")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundColor(.secondary)
                }
                .padding(.vertical, 4)
            }
            .buttonStyle(.plain)
        }
        .listStyle(.plain)
    }
    
    private var emptyState: some View {
        ContentUnavailableView {
            Label("Explore Markets", systemImage: "chart.xyaxis.line")
        } description: {
            Text("Enter a ticker symbol or company name to view interactive real-time charts and intelligence.")
        } actions: {
            Button("Try 'AAPL'") {
                selectedSymbol = "AAPL"
            }
            .buttonStyle(.bordered)
        }
        .frame(maxHeight: .infinity)
    }
    
    func performSearch() {
        guard !searchText.isEmpty else { return }
        isSearching = true
        
        guard let url = URL(string: "http://127.0.0.1:8002/api/search?query=\(searchText.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")") else { return }
        
        Task {
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                let results = try JSONDecoder().decode([SearchResult].self, from: data)
                await MainActor.run {
                    self.searchResults = results
                    self.isSearching = false
                }
            } catch {
                print("Search error: \(error)")
                await MainActor.run { self.isSearching = false }
            }
        }
    }
}

struct SearchResult: Codable, Identifiable {
    var id: String { ticker }
    let ticker: String
    let name: String
}
