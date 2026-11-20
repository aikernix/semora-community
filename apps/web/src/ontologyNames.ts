// Keep stored ontology identifiers stable; translate only at the presentation boundary.
const identifier = String.raw`(?:"(?:""|[^"])+"|\x60(?:\x60\x60|[^\x60])+\x60|\[[^\]]+\]|[\p{L}_$][\p{L}\p{N}_$]*)`
const references = new RegExp(String.raw`--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|${identifier}(?:\s*\.\s*${identifier})*`, 'gu')
const parts = new RegExp(identifier, 'gu')
const unquote = (value: string) => value.startsWith('"') ? value.slice(1,-1).replace(/""/g,'"')
  : value.startsWith('`') ? value.slice(1,-1).replace(/``/g,'`') : value.startsWith('[') ? value.slice(1,-1) : value
const quoted = (value: string) => /^[\p{L}_$][\p{L}\p{N}_$]*$/u.test(value) ? value : `"${value.replace(/"/g,'""')}"`
const key = (value: string) => (value.match(parts)||[]).map(unquote).join('.')

export function ontologyNames(configuration: any = {}, sources: any[] = []) {
  const objects: any[] = configuration?.objects || []
  const sourceNames = new Map((Array.isArray(sources)?sources:[]).map(source=>[source.id,source.name]))
  const forward = new Map<string,string>(), backward = new Map<string,string>()
  const conflicts = new Set<string>()
  function add(internal: string, physical: string, reversible=true) {
    if(!internal || !physical)return
    forward.set(internal,physical)
    if(!reversible)return
    const normalized=key(physical)
    if(backward.has(normalized)&&backward.get(normalized)!==internal){backward.delete(normalized);conflicts.add(normalized)}
    else if(!conflicts.has(normalized))backward.set(normalized,internal)
  }
  for(const object of objects){
    if(!object.table)continue
    let table=object.table
    const sid=object.dataSourceId||configuration.dataSourceId
    if(objects.some(other=>other!==object&&other.table===object.table)){
      const source=sourceNames.get(sid)
      // A source name disambiguates equally named physical tables across databases.
      if(source)table=`${quoted(source)}.${table}`
    }
    add(object.name,table)
    for(const property of object.properties||[]){
      const column=property.column||property.name
      add(`${object.name}.${property.name}`,`${table}.${quoted(column)}`)
      add(property.name,quoted(column),false)
    }
  }
  for(const relation of configuration?.relations||[])if(relation.label)add(relation.name,relation.label,false)
  // Source IDs are not SQL identifiers and are replaced as complete tokens separately.
  const translate=(text:string,map:Map<string,string>)=>String(text||'').replace(references,token=>{
    if(token.startsWith("'")||token.startsWith('--')||token.startsWith('/*'))return token
    return map.get(key(token))||token
  })
  const prose=new Map([...forward].filter(([name])=>/_[a-f0-9]{8}(?:\.|$)/i.test(name)))
  for(const [id,name] of sourceNames)prose.set(id,name)
  const escape=(value:string)=>value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')
  const prosePattern=prose.size?new RegExp(`(?<![A-Za-z0-9_$])(?:${[...prose.keys()].sort((a,b)=>b.length-a.length).map(escape).join('|')})(?![A-Za-z0-9_$])`,'g'):null
  return {
    display:(value:string)=>translate(value,forward),
    text:(value:string)=>{const output=translate(value,forward);return prosePattern?output.replace(prosePattern,token=>prose.get(token)!):output},
    internal:(value:string)=>translate(value,backward),
  }
}
