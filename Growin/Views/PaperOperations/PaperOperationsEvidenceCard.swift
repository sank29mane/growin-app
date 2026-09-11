import SwiftUI

struct PaperOperationsEvidenceRow: Equatable {
    var label: String
    var value: String?
    var help: String?
}

struct PaperOperationsEvidenceCard: View {
    var title: String
    var sessionStopped: Bool
    var rows: [PaperOperationsEvidenceRow]

    var body: some View {
        SovereignCard {
            VStack(alignment: .leading, spacing: 8) {
                Text(title)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                    .foregroundStyle(Color.brutalOffWhite)

                if sessionStopped {
                    Text(PaperOperationsCopy.emptyHeading)
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                        .lineSpacing(8)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    ForEach(rows, id: \.label) { row in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(row.label)
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                                .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                            Text(displayedValue(for: row))
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                                .foregroundStyle(Color.brutalOffWhite)
                                .lineSpacing(8)
                                .fixedSize(horizontal: false, vertical: true)
                                .help(row.help ?? "")
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func displayedValue(for row: PaperOperationsEvidenceRow) -> String {
        guard let value = row.value, !value.isEmpty else {
            return PaperOperationsCopy.missingField
        }
        return value
    }
}
