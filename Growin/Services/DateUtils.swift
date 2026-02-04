import Foundation

struct DateUtils: Sendable {
    // ISO8601DateFormatter is thread-safe on Apple platforms (unlike DateFormatter).
    // We can keep these static.
    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    
    private static let isoStandard: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    
    // DateFormatter is NOT thread-safe. We cannot share instances globally without lock/isolation.
    // We will recreate them or use a thread-local approach. For simplicity and correctness here:
    // we recreate them. Optimized approach would use ThreadDictionary or Locks.
    // Given the context (parsing API response), we can also try a few common patterns manually
    // or just rely on ISO which is standard for this backend.
    
    // Fallback formats
    private static let fallbackFormats = [
        "yyyy-MM-dd'T'HH:mm:ss.SSSSSSZ",
        "yyyy-MM-dd'T'HH:mm:ss.SSSSSS",
        "yyyy-MM-dd'T'HH:mm:ssZ",
        "yyyy-MM-dd'T'HH:mm:ss",
        "yyyy-MM-dd HH:mm:ss",
        "yyyy-MM-dd"
    ]

    static func parse(_ dateString: String) -> Date {
        // 1. Try ISO8601 (Fast, Thread-safe)
        if let date = isoFractional.date(from: dateString) { return date }
        if let date = isoStandard.date(from: dateString) { return date }
        
        // 2. Efficient Double/Timestamp check (very common)
        if let interval = Double(dateString) {
            return Date(timeIntervalSince1970: interval > 10000000000 ? interval / 1000 : interval)
        }
        
        // 3. Fallback to legacy formats (Create formatter locally to ensure thread safety)
        // This is slower but safe. Ideally backend returns ISO.
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        
        for format in fallbackFormats {
            formatter.dateFormat = format
            if let date = formatter.date(from: dateString) { return date }
        }
        
        return Date()
    }
}
