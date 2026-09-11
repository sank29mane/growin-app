import XCTest

final class PaperOperationsBoundaryUITests: XCTestCase {
    @MainActor
    func testPaperOperationsSidebarRowExistsWithoutLiveBrokerChrome() throws {
        let app = XCUIApplication()
        app.launch()

        let paperOperationsRow = app.descendants(matching: .any)["PAPER OPERATIONS"].firstMatch
        XCTAssertTrue(
            paperOperationsRow.waitForExistence(timeout: 5),
            "ContentView Sovereign Alpha sidebar must expose PAPER OPERATIONS (D-01)"
        )
        paperOperationsRow.click()

        let detail = app.windows.firstMatch
        XCTAssertFalse(detail.staticTexts["Breeze"].exists)
        XCTAssertFalse(detail.buttons["Breeze"].exists)
        XCTAssertFalse(detail.staticTexts["Trading 212"].exists)
        XCTAssertFalse(detail.buttons["Trading 212"].exists)
        XCTAssertFalse(detail.staticTexts["live account read"].exists)
        XCTAssertFalse(detail.buttons["live account read"].exists)

        let labels = detail.descendants(matching: .any).allElementsBoundByIndex.compactMap { $0.label }
        for label in labels {
            let lowered = label.lowercased()
            XCTAssertFalse(lowered.contains("breeze"), "Paper Operations must not present Breeze chrome: \(label)")
            XCTAssertFalse(lowered.contains("trading 212"), "Paper Operations must not present Trading 212 chrome: \(label)")
            XCTAssertFalse(lowered.contains("live account read"), "Paper Operations must not present live account-read chrome: \(label)")
        }
    }
}
