import { computed, ref, watch, type Ref } from 'vue'
import { api } from '../api'
import { ontologyNames } from '../ontologyNames'

export function useOntologyNames(domain: Ref<string>) {
  const configuration=ref<any>({}),sources=ref<any[]>([])
  let revision=0
  watch(domain,async value=>{
    const current=++revision
    configuration.value={};sources.value=[]
    if(!value)return
    const results=await Promise.allSettled([api.get(`/ontology/configurations/${encodeURIComponent(value)}`),api.get('/datasources')])
    if(current!==revision)return
    if(results[0].status==='fulfilled')configuration.value=results[0].value.data
    if(results[1].status==='fulfilled'&&Array.isArray(results[1].value.data))sources.value=results[1].value.data
  },{immediate:true})
  const names=computed(()=>ontologyNames(configuration.value,sources.value))
  return (value:string)=>names.value.text(value)
}
