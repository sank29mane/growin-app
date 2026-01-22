# Add Bulk Actions to Chat History

## Current Implementation Analysis

The `ConversationListView` already has a selection system for individual conversations:

- **Edit Mode**: Toggle between normal and edit mode
- **Individual Selection**: Tap conversations to select/deselect
- **Delete Selected**: Delete button appears when items are selected
- **Backend**: Individual delete endpoint (`DELETE /conversations/{id}`)

## Requested Features

Add "Select All" and "Delete All" options to improve bulk management of conversation history.

## Proposed Solution

### 1. UI Enhancements to ConversationListView

**Add bulk action buttons in edit mode toolbar:**

```swift
ToolbarItem {
    HStack {
        // Select All / Deselect All button
        Button(selectAllButtonTitle) {
            toggleSelectAll()
        }
        
        // Delete All button (only when items selected)
        if !selectedIds.isEmpty {
            Button(role: .destructive) {
                showDeleteConfirmation = true
            } label: {
                Image(systemName: "trash")
            }
        }
        
        // Existing Done/Cancel button
        Button(isEditing ? "Done" : "Edit") { ... }
    }
}
```

### 2. Logic Implementation

**Add helper methods to ConversationListViewModel:**

```swift
func selectAllConversations() {
    selectedIds = Set(conversations.map { $0.id })
}

func deselectAllConversations() {
    selectedIds.removeAll()
}

func deleteAllConversations() async {
    await deleteConversations(ids: Array(selectedIds))
}
```

**Add computed property for button title:**

```swift
var selectAllButtonTitle: String {
    selectedIds.count == conversations.count ? "Deselect All" : "Select All"
}
```

### 3. State Management

**Add confirmation dialog state:**
```swift
@State private var showDeleteConfirmation = false
```

**Add confirmation dialog:**
```swift
.alert("Delete All Selected Conversations?", isPresented: $showDeleteConfirmation) {
    Button("Cancel", role: .cancel) { }
    Button("Delete All", role: .destructive) {
        Task { await viewModel.deleteAllConversations() }
    }
} message: {
    Text("This action cannot be undone.")
}
```

### 4. UX Considerations

**Button States:**
- "Select All" when no/few items selected
- "Deselect All" when all items selected  
- "Delete All" only appears when items are selected
- Disable delete when no items selected

**Visual Feedback:**
- Selected conversations show checkmark
- Delete button shows count: "Delete 3"
- Confirmation dialog prevents accidental deletion

### 5. Performance Optimization

**Consider backend bulk delete endpoint:**
- Current: Multiple individual DELETE requests
- Proposed: Single bulk DELETE request with array of IDs
- Benefits: Reduced network calls, atomic operation

## Implementation Steps

### Phase 1: Frontend UI Updates
1. **Modify toolbar** - Add Select All/Deselect All button
2. **Update delete logic** - Add Delete All with confirmation
3. **Add state management** - Confirmation dialog and button states

### Phase 2: Backend Optimization (Optional)
1. **Add bulk delete endpoint** - `DELETE /conversations/bulk` with JSON array
2. **Update frontend** - Use bulk endpoint for multiple deletions
3. **Maintain backward compatibility** - Keep individual delete working

### Phase 3: Testing and Polish
1. **Test all scenarios** - Select all, deselect all, delete all, mixed selection
2. **Verify UX** - Button states, confirmations, loading states
3. **Edge cases** - Empty list, single item, network errors

## Files to Modify

### Frontend
- `/Views/ConversationListView.swift` - Add bulk action UI and logic

### Backend (Optional)
- `/routes/chat_routes.py` - Add bulk delete endpoint
- `/chat_manager.py` - Implement bulk delete logic

## Success Criteria

✅ **Select All** - Button toggles between selecting/deselecting all conversations
✅ **Delete All** - Shows confirmation dialog and deletes all selected conversations  
✅ **UX** - Clear button states, proper feedback, confirmation dialogs
✅ **Performance** - Efficient bulk operations
✅ **Safety** - Confirmation required for destructive actions

## Risk Assessment

- **Low Risk**: Building on existing selection/delete infrastructure
- **UI Only**: No backend changes required for basic functionality
- **Backward Compatible**: Existing individual selection/delete still works
- **Performance**: Minimal impact, optional bulk endpoint for optimization

## Alternative Approaches

1. **Inline bulk actions**: Add buttons directly in the list view
2. **Swipe actions**: iOS-style swipe to delete with bulk options
3. **Context menu**: Long-press menu with bulk options

The toolbar approach maintains consistency with existing iOS design patterns and provides clear, accessible bulk actions.</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/add-bulk-chat-actions.md