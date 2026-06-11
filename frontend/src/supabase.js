import { createClient } from '@supabase/supabase-js';

const supabaseUrl = 'https://utchzaroqrvhimzsbags.supabase.co';
const supabaseKey = 'sb_publishable_DxFjGJTASV0pyAnc71XJVg_ctRzypOX';

export const supabase = createClient(supabaseUrl, supabaseKey);
