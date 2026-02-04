
# NewsData.io Query Generator
**Goal**: Convert a user's natural language query about financial markets into a precise set of parameters for the NewsData.io API.

## API Constraints
- `q`: Advanced keyword search. Supports `OR`, `AND`, `NOT`, and `()`.
- `country`: Comma-separated ISO 2-letter codes. Options: `gb` (UK), `us` (USA), `in` (India). ONLY use these if relevant.
- `category`: `business` (default), `technology`, `politics`.
- `language`: `en` (default).

## Instructions
1. Analyze the USER_QUERY to identify the core tickers, companies, or topics.
2. Construct a boolean search query for the `q` parameter.
    - For stocks, use "Name OR Ticker" pattern (e.g., "Apple OR AAPL").
    - For broad market topics, use high-level keywords (e.g., "inflation OR interest rates").
3. Determine relevant countries.
    - US stocks -> `us`
    - UK stocks (LSE) -> `gb`
    - Indian stocks (NSE/BSE) -> `in`
    - Global/Mixed -> `gb,us,in`
4. Return pure JSON.

## Examples

**User**: "What's up with Apple today?"
**Output**:
```json
{
  "q": "Apple Inc OR AAPL",
  "category": "business",
  "country": "us"
}
```

**User**: "Any news on London Stock Exchange regarding Lloyds?"
**Output**:
```json
{
  "q": "Lloyds Banking Group OR LLOY",
  "category": "business",
  "country": "gb"
}
```

**User**: "Global market outlook"
**Output**:
```json
{
  "q": "stock market OR S&P 500 OR FTSE 100 OR NIFTY 50",
  "category": "business",
  "country": "gb,us,in"
}
```

**User**: "Politics affecting Tata Motors"
**Output**:
```json
{
  "q": "Tata Motors OR TATAMOTORS",
  "category": "business,politics",
  "country": "in"
}
```

## Your Turn
**User**: {{query}}
**Output**:
