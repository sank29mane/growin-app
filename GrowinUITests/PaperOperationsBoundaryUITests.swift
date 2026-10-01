import XCTest

final class PaperOperationsBoundaryUITests: XCTestCase {
    @MainActor
    func testPaperOperationsSidebarRowExistsWithoutLiveBrokerChrome() throws {
        let app = XCUIApplication()
        app.launch()

        let paperOperationsRow = app.descendants(matching: .any)["PAPER OPERATIONS"].firstMatch
        XCTAssertTrue(
            paperOperationsRow.waitForExistence(timeout: 8),
            "ContentView Sovereign Alpha sidebar must expose PAPER OPERATIONS (D-01)"
        )
        paperOperationsRow.click()

        XCTAssertTrue(
            app.staticTexts["PAPER OPERATIONS"].waitForExistence(timeout: 5),
            "Paper Operations detail must show the PAPER OPERATIONS heading"
        )
        XCTAssertTrue(
            app.staticTexts["PAPER ONLY · LOCAL REPLAY · NO BROKER"].waitForExistence(timeout: 5),
            "Paper Operations detail must show the PAPER ONLY mode strip"
        )

        let start = app.buttons["Start Local Replay"]
        let prepare = app.buttons["Prepare Paper Intent"]
        let acknowledge = app.buttons["Acknowledge Local Fill"]
        let reconcile = app.buttons["Reconcile Paper Outcome"]
        XCTAssertTrue(start.waitForExistence(timeout: 5), "Start Local Replay must remain visible")
        XCTAssertTrue(prepare.exists, "Prepare Paper Intent must remain visible when not the next action")
        XCTAssertTrue(acknowledge.exists, "Acknowledge Local Fill must remain visible when not the next action")
        XCTAssertTrue(reconcile.exists, "Reconcile Paper Outcome must remain visible when not the next action")
        XCTAssertTrue(start.isEnabled, "Start Local Replay is the next-safe-action while stopped")
        XCTAssertFalse(prepare.isEnabled, "Prepare stays disabled until evidence is complete")
        XCTAssertFalse(acknowledge.isEnabled, "Acknowledge stays visible and disabled until signed")
        XCTAssertFalse(reconcile.isEnabled, "Reconcile stays visible and disabled until acknowledged")

        XCTAssertFalse(app.buttons["Breeze"].exists)
        XCTAssertFalse(app.staticTexts["Breeze"].exists)
        XCTAssertFalse(app.buttons["Trading 212"].exists)
        XCTAssertFalse(app.staticTexts["Trading 212"].exists)
        XCTAssertFalse(app.buttons["live account"].exists)
        XCTAssertFalse(app.staticTexts["live account"].exists)
        XCTAssertFalse(app.staticTexts["live account read"].exists)
        XCTAssertFalse(app.buttons["live account read"].exists)

        XCTAssertFalse(app.toggles["Auto-refresh"].exists)
        XCTAssertFalse(app.switches["Auto-refresh"].exists)
        XCTAssertFalse(app.checkBoxes["Auto-refresh"].exists)
        XCTAssertFalse(app.buttons["Auto-refresh"].exists)

        let controlQueries: [XCUIElementQuery] = [
            app.buttons,
            app.toggles,
            app.switches,
            app.checkBoxes,
        ]
        for query in controlQueries {
            let labels = query.allElementsBoundByIndex.compactMap { $0.label }
            for label in labels {
                let lowered = label.lowercased()
                XCTAssertFalse(lowered.contains("breeze"), "Paper Operations must not present Breeze chrome: \(label)")
                XCTAssertFalse(lowered.contains("trading 212"), "Paper Operations must not present Trading 212 chrome: \(label)")
                XCTAssertFalse(lowered.contains("live account"), "Paper Operations must not present live account-read chrome: \(label)")
                XCTAssertFalse(lowered.contains("auto-refresh"), "Paper Operations must not present auto-refresh: \(label)")
            }
        }
    }
}
