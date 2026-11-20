export interface FieldPair { source:string; target:string }

// Only decompose an entire, simple conjunction. Complex existing SQL stays intact.
export function fieldPairs(condition:string,from:string,to:string):FieldPair[]|null {
  const pairs:FieldPair[]=[]
  for(const clause of (condition||'').split(/\s+AND\s+/i)){
    const match=clause.trim().match(/^"?([\p{L}\p{N}_$]+)"?\s*\.\s*"?([\p{L}\p{N}_$]+)"?\s*=\s*"?([\p{L}\p{N}_$]+)"?\s*\.\s*"?([\p{L}\p{N}_$]+)"?$/u)
    if(!match)return null
    if(match[1]===from&&match[3]===to)pairs.push({source:match[2]!,target:match[4]!})
    else if(match[1]===to&&match[3]===from)pairs.push({source:match[4]!,target:match[2]!})
    else return null
  }
  return pairs.length?pairs:null
}

export function mappingCondition(from:string,to:string,pairs:FieldPair[]):string {
  return pairs.filter(pair=>pair.source&&pair.target).map(pair=>`${from}.${pair.source} = ${to}.${pair.target}`).join(' AND ')
}
