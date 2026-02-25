import Foundation

struct SSEParser {
    static func parseLine(_ line: String) -> (event: String?, data: String?) {
        guard !line.isEmpty else { return (nil, nil) }
        
        let components = line.split(separator: ":", maxSplits: 1).map { String($0).trimmingCharacters(in: .whitespaces) }
        guard components.count >= 2 else { return (nil, nil) }
        
        let key = components[0]
        let value = components[1]
        
        if key == "event" {
            return (value, nil)
        } else if key == "data" {
            return (nil, value)
        }
        
        return (nil, nil)
    }
}
