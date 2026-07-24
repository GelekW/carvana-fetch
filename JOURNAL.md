- First thing I am going to do is just open up devtools in carvana.com.
- Clicked on the search button and copied the payload I can see:
```
{analyticsData: {browser: "Chrome", clientId: "srp_ui", deviceName: "", isBot: false,…},…}
analyticsData
...
null
filters
...
zip5
: 
"63118"
```
- Went over to the search request preview found this:
```
inventory: {
  pagination: {
    currentPage: 1,
    pageSize: 24,
    totalMatchedInventory: 68800,
    totalMatchedPages: 2992
  },
  vehicles: [...]
}
```
- So they tell us the tot pages and tot inventory
- I think for fun I'll make it a CI job and zip code for search can just come in as build inputs.
- Did some googling. It seems you want to 
  1. Stagger API calls at a random rate, not uniform
  2. Mimic a browser session, send browser (i.e. Chrome in request, isBot: false), some header spoofing so this doesn't look like it was ran by a python script.
  3. Persist with same identifiers throughout
  4. Use chromes tls handshake fingerprint instead of `requests`
  5. Proxy the IP so github runner range isn't recognized
- initial design for getting past the depth cap:
  - 2992 pages is a lie, they'll cut me off way before (~10k then repeats)
  - slice by price into chunks I can pull fully, halve when too big, dedupe on VIN
  - check: tot VINs vs 68800, maybe re-slice by year
- rough ideas, sure the llm can patch whatever I'm missing
